"""The MCP gateway: Purse's real public surface (C4.1-C4.3, C4.6-C4.7; PRD §10, §12).

The REST app (:mod:`purse.gateway.rest`) was the M1 smoke path. This module is
the product's front door — a FastMCP server exposing the five memory tools and
the three skills tools over Streamable HTTP, workspace-scoped by the authenticated
connection, with the structured error envelope PRD §10 fixes.

.. rubric:: Stateless by design

The MCP spec 2026-07-28 removes sessions (spike verdict, TASKS.md): no
``Mcp-Session-Id``, no ``ctx.sample()`` / ``ctx.list_roots()``. Every tool keys
its behaviour off the **verified token** and nothing else, so the server holds no
per-connection state between calls. The transport is configured stateless when the
ASGI app is built (:func:`create_mcp_http_app`, ``stateless_http=True``).

.. rubric:: The trust boundary

Each tool resolves its workspace from the token's verified claims
(:func:`_resolve_caller`), *never* from a tool argument — no tool accepts
``workspace_id`` or ``connection_id`` (PRD §10). ``connection_id`` from the token
is the trusted provenance recorded on every write; ``initiated_by`` is a
self-reported claim, trusted no further than being stored (PRD §10, C4.7).

.. rubric:: The injection seam

:func:`create_mcp_server` takes ``auth`` — any :class:`fastmcp...AuthProvider`.
In production the parallel OAuth agent supplies its ``OAuthProvider`` (which *is*
an ``AuthProvider``); its ``verify_token`` must return an ``AccessToken`` whose
``scopes`` are the connection's granted scopes and whose ``claims`` carry
``connection_id``, ``workspace_id``, ``writes_enabled`` (and optionally
``client_name``). Tests inject a tiny ``TokenVerifier`` stub with the same claim
shape, so this module never imports the OAuth server. See the module tests and
the C4 report for the exact contract.
"""

from __future__ import annotations

import contextlib
import enum
import json
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token
from sqlalchemy.orm import Session

from purse.auth.context import AuthContext
from purse.auth.context import require_scope as auth_require_scope
from purse.auth.errors import ScopeError
from purse.auth.oauth.claims import (
    CLAIM_CLIENT_NAME,
    CLAIM_CONNECTION_ID,
    CLAIM_WORKSPACE_ID,
    CLAIM_WRITES_ENABLED,
)
from purse.auth.scopes import Scope
from purse.gateway.ratelimit import RateLimiter, RateLimitExceeded
from purse.memory import service
from purse.memory.engine import MemoryEngine
from purse.memory.errors import MemoryError_
from purse.memory.records import SearchHit
from purse.skills import service as skills_service
from purse.skills.errors import SkillError

__all__ = [
    "MCPErrorCode",
    "MCPToolError",
    "create_mcp_http_app",
    "create_mcp_server",
]

#: The MCP endpoint path. The orchestrator mounts the Streamable HTTP app here so
#: MCP, REST, and the OAuth routes can share one port (see the C4 report).
DEFAULT_MCP_PATH = "/mcp"


class MCPErrorCode(enum.StrEnum):
    """The structured error codes PRD §10 fixes for the MCP surface.

    Three are reachable from the memory tools (``UNAUTHORIZED_SCOPE``,
    ``NOT_FOUND``, ``PAYLOAD_TOO_LARGE``); ``RATE_LIMITED`` (C2.10) and
    ``HOST_NOT_ALLOWED`` (C6) belong to later clusters and are declared here so
    the envelope has one home for every code. ``VALIDATION`` and
    ``UNAUTHENTICATED`` are not in §10's tool-error list — the first is what the
    memory service raises for a bad enum/limit, the second is the defensive code
    for a token that verified but carries no Purse claims (a provider bug).
    """

    UNAUTHORIZED_SCOPE = "UNAUTHORIZED_SCOPE"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    VALIDATION = "VALIDATION"
    UNAUTHENTICATED = "UNAUTHENTICATED"


class MCPToolError(ToolError):
    """A tool failure carrying PRD §10's ``{error: {code, message}}`` envelope.

    ``ToolError`` is the FastMCP mechanism for a tool-visible failure: the server
    returns it as an ``isError`` result rather than a raw JSON-RPC error, and it
    survives ``mask_error_details`` (only *unexpected* exceptions are masked).
    FastMCP puts ``str(exc)`` into the result's text content, so the wire
    representation is exactly the §10 envelope, serialised as JSON. ``.code`` and
    ``.error_message`` stay available for in-process assertions.
    """

    def __init__(self, code: MCPErrorCode | str, message: str) -> None:
        self.code = str(code)
        self.error_message = message
        super().__init__(json.dumps({"error": {"code": self.code, "message": message}}))


# ---------------------------------------------------------------------------
# The authenticated caller, derived from the verified token
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ToolContext:
    """One tool call's provenance: the verified connection plus the agent claim.

    Satisfies :class:`~purse.memory.context.WriteContext` structurally (the shape
    the memory service takes), mirroring :class:`purse.gateway.rest.RequestContext`.
    ``caller`` is what the token proved; ``agent_id`` is the per-call claim.

    ``agent_id`` is always ``None`` on the MCP surface. REST carries it in the
    ``X-Purse-Agent`` header (PRD §10 leaves ``agent_id`` to the transport), but
    an MCP tool call has no equivalent per-call header, and the MCP client's
    declared name is not a trustworthy agent identity. The trusted provenance —
    ``connection_id`` — is always recorded; the optional ``agent_id`` claim is
    simply absent here, which is a shape PRD §10 already allows.
    """

    caller: AuthContext
    agent_id: str | None = None

    @property
    def connection_id(self) -> uuid.UUID:
        return self.caller.connection_id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.caller.workspace_id


def _known_scopes(raw: Iterable[str]) -> frozenset[Scope]:
    """The subset of *raw* that are recognised Purse scopes.

    A real OAuth token may carry scopes Purse does not model (``openid`` and the
    like); an unknown scope is ignored rather than failing the whole request. The
    parse is otherwise exact — a value must equal a :class:`Scope` member — so a
    typo grants nothing rather than something adjacent.
    """
    scopes: set[Scope] = set()
    for value in raw:
        try:
            scopes.add(Scope(value))
        except ValueError:
            continue
    return frozenset(scopes)


def _resolve_caller() -> AuthContext:
    """The verified caller for the current tool call, or raise ``UNAUTHENTICATED``.

    Reads the access token the auth middleware attached (FastMCP's
    ``get_access_token()``), and rebuilds the connection's identity from its
    verified fields — ``scopes`` from the standard OAuth claim, and
    ``connection_id`` / ``workspace_id`` / ``writes_enabled`` from the provider's
    ``claims``. The workspace comes from here and nowhere else (PRD §10).
    """
    token = get_access_token()
    if token is None:
        # Behind the auth middleware this cannot happen; if it does, the server
        # was built with auth=None, and refusing is the only safe answer.
        raise MCPToolError(MCPErrorCode.UNAUTHENTICATED, "no authenticated connection")

    # Claim keys are the namespaced constants defined once in purse.auth.oauth.claims
    # (the token builder both the OAuth provider and the PAT verifier mint through),
    # so this stays in lockstep with whatever those providers write. Scopes keep the
    # forgiving filter below — real OAuth tokens may carry standard scopes (openid,
    # profile) outside Purse's vocabulary, which must not fail the request.
    claims = token.claims or {}
    try:
        connection_id = uuid.UUID(str(claims[CLAIM_CONNECTION_ID]))
        workspace_id = uuid.UUID(str(claims[CLAIM_WORKSPACE_ID]))
        writes_enabled = bool(claims[CLAIM_WRITES_ENABLED])
    except (KeyError, ValueError, TypeError) as exc:
        raise MCPToolError(
            MCPErrorCode.UNAUTHENTICATED,
            "token is missing the Purse connection claims",
        ) from exc

    client_name = str(claims.get(CLAIM_CLIENT_NAME) or token.client_id)
    return AuthContext(
        connection_id=connection_id,
        workspace_id=workspace_id,
        scopes=_known_scopes(token.scopes),
        writes_enabled=writes_enabled,
        client_name=client_name,
    )


def _require(caller: AuthContext, scope: Scope) -> None:
    """Enforce *scope* on *caller*, translating the auth failure to §10's code.

    ``auth_require_scope`` also gates ``:write`` scopes on ``writes_enabled`` —
    the "writes on" badge (PRD §7.1) — so a read-only connection is refused a
    write even when the scope was granted.
    """
    try:
        auth_require_scope(caller, scope)
    except ScopeError as exc:
        raise MCPToolError(MCPErrorCode.UNAUTHORIZED_SCOPE, str(exc)) from exc


def _limit_write(limiter: RateLimiter | None, caller: AuthContext) -> None:
    """Charge one write against *caller*'s per-connection budget (PRD §13, C2.10).

    Called after the scope check and before the service write, so a refused
    caller never touches the database. ``limiter is None`` disables limiting —
    the default for the in-memory/registration tests and any call site that does
    not opt in; the assembled app (:mod:`purse.gateway.asgi`) always passes one.
    """
    if limiter is None:
        return
    try:
        limiter.check(caller.connection_id)
    except RateLimitExceeded as exc:
        raise MCPToolError(MCPErrorCode.RATE_LIMITED, str(exc)) from exc


@contextlib.contextmanager
def _unit_of_work(session_factory: Callable[[], Session]) -> Iterator[Session]:
    """One request, one transaction — commit on success, roll back on any error.

    Mirrors the REST app's ``db_session`` dependency. Memory-service functions
    flush but never commit (they leave the boundary to the caller), so this is
    where a tool's write becomes durable, and where a failed audit can never
    strand a memory row.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextlib.contextmanager
def _mapped_errors() -> Iterator[None]:
    """Translate service errors into the §10 envelope; pass §10 errors through.

    The memory and skills services raise error codes (``NOT_FOUND``,
    ``PAYLOAD_TOO_LARGE``, ``VALIDATION``) that are already the wire codes, so the
    mapping is a pass-through of ``exc.code`` for either hierarchy — both expose
    the same ``.code`` / ``.message`` pair. An :class:`MCPToolError` raised
    upstream (scope denial, ``UNAUTHENTICATED``) is re-raised unchanged.
    """
    try:
        yield
    except MCPToolError:
        raise
    except (MemoryError_, SkillError) as exc:
        raise MCPToolError(exc.code, exc.message) from exc


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_mcp_server(
    *,
    session_factory: Callable[[], Session],
    engine: MemoryEngine,
    auth: AuthProvider | None,
    limiter: RateLimiter | None = None,
    name: str = "Purse",
) -> FastMCP:
    """Build the FastMCP server with the five memory tools and three skills tools.

    :param session_factory: Called once per tool call; the session is committed
        on success and rolled back on any error.
    :param engine: The derived index (:class:`~purse.memory.engine.NullEngine`
        until the Mem0 adapter lands, C3.4). Shared across calls — must be
        thread-safe (tools run in a worker thread by default).
    :param auth: The FastMCP auth provider that verifies bearer tokens. In
        production this is the OAuth agent's ``OAuthProvider``; in tests it is a
        ``TokenVerifier`` stub. Its ``verify_token`` must yield an ``AccessToken``
        whose ``scopes`` are the connection's granted scopes and whose ``claims``
        carry ``connection_id``, ``workspace_id`` and ``writes_enabled``. ``None``
        builds an unauthenticated server (every tool then refuses with
        ``UNAUTHENTICATED``) — useful only for schema/registration tests over the
        in-memory transport, which does not run the HTTP auth stack.

    :param limiter: The shared per-connection write limiter (PRD §13, C2.10).
        ``None`` disables limiting — the default, so registration/in-memory tests
        are unaffected. The assembled app passes ONE instance shared with the REST
        surface (:mod:`purse.gateway.asgi`) so a connection's write budget is
        unified across both.

    The returned server is transport-agnostic; :func:`create_mcp_http_app` wraps
    it as a stateless Streamable HTTP ASGI app.
    """
    # mask_error_details stays on: an unexpected exception (a real bug) must not
    # leak internals to a possibly-hostile client. Our MCPToolError is a
    # FastMCPError, which FastMCP re-raises before the masking path, so the §10
    # envelope always reaches the client intact.
    mcp: FastMCP = FastMCP(name=name, auth=auth, mask_error_details=True)

    @mcp.tool
    def search_memory(query: str, limit: int = service.DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        """Search this workspace's current memories, best match first.

        Returns ranked facts, each ``{id, content, created_at, provenance}``.
        Scope: ``memory:read``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.MEMORY_READ)
        ctx = _ToolContext(caller=caller)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            hits = service.search_memory(session, ctx, engine, query=query, limit=limit)
            return {"results": [_fact(hit) for hit in hits]}

    @mcp.tool
    def add_memory(
        content: str,
        kind: Literal["fact", "preference", "decision"],
        initiated_by: Literal["user", "agent"],
    ) -> dict[str, Any]:
        """Append a durable memory to this workspace (verbatim, provenanced).

        ``content`` is capped at 4 KB of UTF-8 (``PAYLOAD_TOO_LARGE`` beyond that).
        ``initiated_by`` is a self-reported claim; the trusted provenance is the
        token's connection. Returns ``{id, created_at}``. Scope: ``memory:write``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.MEMORY_WRITE)
        _limit_write(limiter, caller)
        ctx = _ToolContext(caller=caller)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            record = service.add_memory(
                session, ctx, engine, content=content, kind=kind, initiated_by=initiated_by
            )
            return {"id": str(record.id), "created_at": record.created_at.isoformat()}

    @mcp.tool
    def list_memories(
        cursor: str | None = None, limit: int = service.DEFAULT_LIST_LIMIT
    ) -> dict[str, Any]:
        """A page of this workspace's current memories, newest first.

        ``cursor`` is the opaque ``next_cursor`` from a previous page (``None``
        for the first). Returns ``{items, next_cursor}``. Scope: ``memory:read``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.MEMORY_READ)
        ctx = _ToolContext(caller=caller)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            page = service.list_memories(session, ctx, cursor=cursor, limit=limit)
            return {
                "items": [record.as_dict() for record in page.items],
                "next_cursor": page.next_cursor,
            }

    @mcp.tool
    def update_memory(id: str, content: str) -> dict[str, Any]:  # PRD §10 names this param `id`
        """Supersede a current memory with new content (append, never mutate).

        Writes a new row pointing at the old one and returns the **new** ``id``.
        Only a current memory can be superseded; anything else is ``NOT_FOUND``.
        Scope: ``memory:write``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.MEMORY_WRITE)
        _limit_write(limiter, caller)
        ctx = _ToolContext(caller=caller)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            record = service.update_memory(
                session, ctx, engine, memory_id=_parse_id(id), content=content
            )
            return {"id": str(record.id)}

    @mcp.tool
    def delete_memory(id: str) -> dict[str, Any]:  # PRD §10 names this param `id`
        """Tombstone a memory (idempotent; the row survives as history).

        Returns ``{id, deleted}``. Unknown ids are ``NOT_FOUND``.
        Scope: ``memory:write``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.MEMORY_WRITE)
        _limit_write(limiter, caller)
        ctx = _ToolContext(caller=caller)
        memory_id = _parse_id(id)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            service.delete_memory(session, ctx, memory_id=memory_id, engine=engine)
            return {"id": str(memory_id), "deleted": True}

    # -- skills tools (C4.4 / C5.3) -----------------------------------------

    @mcp.tool
    def list_skills() -> dict[str, Any]:
        """List this workspace's skills — the latest version of each.

        Returns ``{skills: [{name, description, version}]}``. Scope: ``skills:read``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.SKILLS_READ)
        ctx = _ToolContext(caller=caller)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            summaries = skills_service.list_skills(session, ctx)
            return {"skills": [summary.as_dict() for summary in summaries]}

    @mcp.tool
    def get_skill(name: str, version: str | None = None) -> dict[str, Any]:
        """Fetch a skill's frontmatter and body.

        ``version`` defaults to the latest; a specific version fetches that one.
        An unknown skill or version is ``NOT_FOUND``. Returns the skill's
        frontmatter and markdown body. Scope: ``skills:read``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.SKILLS_READ)
        ctx = _ToolContext(caller=caller)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            record = skills_service.get_skill(session, ctx, name=name, version=version)
            return record.as_dict()

    @mcp.tool
    def upsert_skill(name: str, content: str) -> dict[str, Any]:
        """Create or update a skill from a markdown document with YAML frontmatter.

        The frontmatter ``name`` must equal *name*. Re-upserting an existing
        version with identical content is a no-op; the same version with different
        content is rejected (``VALIDATION``) — bump the version. Documents are
        capped at 64 KB (``PAYLOAD_TOO_LARGE`` beyond that). Returns
        ``{name, version}``. Scope: ``skills:write``.
        """
        caller = _resolve_caller()
        _require(caller, Scope.SKILLS_WRITE)
        _limit_write(limiter, caller)
        ctx = _ToolContext(caller=caller)
        with _mapped_errors(), _unit_of_work(session_factory) as session:
            record = skills_service.upsert_skill(session, ctx, name=name, content=content)
            return {"name": record.name, "version": record.version}

    return mcp


def create_mcp_http_app(server: FastMCP, *, path: str = DEFAULT_MCP_PATH) -> Any:
    """The stateless Streamable HTTP ASGI app for *server* (PRD §12; C4.1).

    ``stateless_http=True`` is the spike directive made concrete: a fresh
    transport per request, no ``Mcp-Session-Id`` reliance. The returned Starlette
    app carries its own ``lifespan``; a parent FastAPI/Starlette app that mounts
    it must chain that lifespan (see the C4 report for the mount recipe).
    """
    return server.http_app(path=path, transport="http", stateless_http=True)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _fact(hit: SearchHit) -> dict[str, Any]:
    """A search hit as PRD §10's ranked fact: ``{id, content, created_at, provenance}``."""
    return {
        "id": str(hit.id),
        "content": hit.content,
        "created_at": hit.created_at.isoformat(),
        "provenance": hit.provenance.as_dict(),
    }


def _parse_id(value: str) -> uuid.UUID:
    """Parse a memory id, surfacing a bad one as ``NOT_FOUND`` rather than a 500.

    A malformed id is, from the caller's point of view, an id that is not a memory
    they can act on — the same answer as an unknown one, and the same answer that
    keeps a nonexistent id from being distinguishable from another workspace's.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise MCPToolError(MCPErrorCode.NOT_FOUND, f"memory {value!r} not found") from exc
