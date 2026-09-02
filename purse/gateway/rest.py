"""REST memory endpoints — the pre-MCP smoke path (C3.8, PRD §15 M1).

M1's job is to prove the spine end to end before MCP exists: sign up, get a
token, ``POST /v1/memories``, ``GET /v1/memories/search`` from a different
process, see the memory come back. That is a *test surface first* — it exercises
auth → scope → canonical insert → audit → engine (PRD §12) over plain HTTP,
where curl is the client and failures are legible.

The MCP gateway (C4) is the real public surface and will call the same
:mod:`purse.memory.service` functions with the same error codes. Nothing here is
throwaway, but nothing here is the product either.

.. rubric:: What this module deliberately does not do

* **No server.** ``create_app`` returns an ASGI app; ``uvicorn`` is not a
  dependency. Serving it — process manager, workers, TLS, the compose
  entrypoint — lands with C4/C9 self-hosting.
* **No authentication.** ``authenticate`` is injected. C2 owns tokens.
* **No scope policy.** ``require_scope`` is injected and defaults to a no-op, so
  this module can be exercised before C2 lands. Enforcement is the auth
  module's job — see :func:`create_app` for the wiring.

.. rubric:: Wiring ``purse.auth`` (C2)

Both hooks are injected, so this module never imports ``purse.auth`` and the two
packages can be built and tested independently. The orchestrator supplies two
small adapters — small because ``AuthContext`` already satisfies
:class:`GatewayContext` structurally; only the exception types and the scope
enum need translating::

    from purse.auth.context import require_scope as auth_require_scope
    from purse.auth.errors import AuthenticationError
    from purse.auth.errors import ScopeError as AuthScopeError
    from purse.auth.pat import authenticate_pat
    from purse.auth.scopes import Scope
    from purse.db.session import create_db_engine, session_factory
    from purse.gateway.errors import ScopeError, UnauthorizedError
    from purse.gateway.rest import create_app
    from purse.memory import NullEngine


    def authenticate(session, token):
        try:
            return authenticate_pat(session, token)
        except AuthenticationError as exc:
            raise UnauthorizedError(str(exc)) from exc


    def require_scope(ctx, scope):
        try:
            auth_require_scope(ctx, Scope(scope))
        except AuthScopeError as exc:
            raise ScopeError(str(exc)) from exc


    engine = create_db_engine()
    app = create_app(
        session_factory=session_factory(engine),
        engine=NullEngine(),  # Mem0 adapter in C3.4
        authenticate=authenticate,
        require_scope=require_scope,
    )

Why adapters rather than passing ``authenticate_pat`` and ``auth_require_scope``
directly:

* ``purse.auth`` raises ``AuthError`` subclasses carrying an ``ErrorCode`` but no
  HTTP status, because HTTP is not its concern. Translating them here is what
  keeps the status-code table in one place — this module.
* ``auth_require_scope`` takes a ``Scope`` enum member; the gateway names scopes
  as plain strings so it does not depend on the auth vocabulary. ``Scope(scope)``
  is the whole conversion, and it raises loudly on a scope this module invented
  and ``purse.auth`` does not know about, which is the right failure.
"""

# NOTE: no `from __future__ import annotations` in this module, and that is
# load-bearing rather than an oversight.
#
# PEP 563 turns every annotation into a string, and FastAPI resolves those
# strings against the *module* globals of the decorated function. The endpoints
# below are nested inside `create_app` and their `Annotated[..., Depends(x)]`
# markers reference `caller` and `db_session`, which are closure locals — under
# PEP 563 they would be unresolvable forward references and every request would
# die at import. Evaluating annotations eagerly, as this module does, is what
# makes the `Annotated` dependency style usable here at all.
#
# Everything annotated in this file is valid at runtime on 3.12 (`X | None`,
# builtin generics), so nothing is lost by leaving the future import out.

import math
import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from purse.gateway.errors import GatewayError, RateLimitedError, UnauthorizedError
from purse.gateway.ratelimit import RateLimiter, RateLimitExceeded
from purse.memory import service
from purse.memory.engine import MemoryEngine
from purse.memory.errors import MemoryError_

__all__ = [
    "AGENT_HEADER",
    "MAX_AGENT_ID_LENGTH",
    "MEMORY_READ",
    "MEMORY_WRITE",
    "Authenticate",
    "GatewayContext",
    "RequestContext",
    "RequireScope",
    "create_app",
]

MEMORY_READ = "memory:read"
MEMORY_WRITE = "memory:write"

_STATUS_BY_CODE = {
    "NOT_FOUND": 404,
    "PAYLOAD_TOO_LARGE": 413,
    "VALIDATION": 422,
}


# ---------------------------------------------------------------------------
# Injected contracts
# ---------------------------------------------------------------------------


class GatewayContext(Protocol):
    """What a verified token resolves to.

    Deliberately the *connection's* properties and nothing else. It does **not**
    require ``agent_id``, because a token does not identify an agent: PRD §10
    makes ``agent_id`` a self-reported per-call claim, and a connection used by
    two agents would otherwise have to authenticate twice. The gateway attaches
    that claim per request — see :class:`RequestContext`.

    Structural, so ``purse.auth``'s ``AuthContext`` satisfies it exactly as
    written (its ``scopes`` is a ``frozenset[Scope]``, and ``Scope`` is a
    ``StrEnum``, hence a ``Collection[str]``) with no import in either
    direction.
    """

    @property
    def connection_id(self) -> uuid.UUID:
        """The authenticated connection — the trusted provenance (PRD §10, C4.7)."""
        ...

    @property
    def workspace_id(self) -> uuid.UUID:
        """The workspace this connection is bound to. The isolation boundary."""
        ...

    @property
    def scopes(self) -> Collection[str]:
        """Granted scopes, e.g. ``{"memory:read", "memory:write"}`` (C2.2)."""
        ...

    @property
    def writes_enabled(self) -> bool:
        """The "writes on" badge (PRD §7.1). Read-only connections have this false."""
        ...


#: Header carrying the caller's optional ``agent_id`` claim.
#:
#: REST has nowhere else to put it: PRD §10 gives the MCP tools ``initiated_by``
#: as an explicit parameter but leaves ``agent_id`` to the transport, and putting
#: it in the request body would mean adding it to four different schemas for a
#: field that describes the caller rather than the memory. When the MCP gateway
#: lands (C4.3) it will populate the same field from its own call metadata.
AGENT_HEADER = "X-Purse-Agent"

#: ``agent_id`` is a ``text`` column that lands in audit rows; a caller should not
#: be able to write a megabyte into one by setting a header.
MAX_AGENT_ID_LENGTH = 128


@dataclass(frozen=True)
class RequestContext:
    """One request's provenance: the verified connection plus the agent's claim.

    Satisfies :class:`~purse.memory.context.WriteContext`, which is what the
    memory service takes. The split is the point — ``caller`` is what
    authentication proved and what scope checks read; ``agent_id`` is what the
    caller said about itself and is trusted no further than being recorded.
    """

    caller: GatewayContext
    agent_id: str | None

    @property
    def connection_id(self) -> uuid.UUID:
        return self.caller.connection_id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.caller.workspace_id


#: Resolve a bearer token to a caller, or raise ``UnauthorizedError``.
Authenticate = Callable[[Session, str], GatewayContext]

#: Assert a caller holds a scope, or raise ``ScopeError``.
RequireScope = Callable[[GatewayContext, str], None]


def _allow_every_scope(ctx: GatewayContext, scope: str) -> None:
    """The default ``require_scope``: permits everything.

    Not a security decision — a placeholder so C3 is testable before C2 exists.
    Every endpoint below already names the scope it needs, so switching to real
    enforcement is one argument at the wiring site.
    """
    return None


def _agent_claim(request: Request) -> str | None:
    """The ``X-Purse-Agent`` header, normalised. Absent or blank becomes ``None``.

    Over-long values are truncated rather than rejected: this is a courtesy
    label on a request that is otherwise perfectly valid, and failing a memory
    write because a client sent a verbose user-agent would be the wrong trade.
    """
    raw = (request.headers.get(AGENT_HEADER) or "").strip()
    return raw[:MAX_AGENT_ID_LENGTH] if raw else None


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    # An unknown field is a client bug — a typo'd `initated_by` that silently
    # became the default would be a provenance error nobody notices.
    model_config = ConfigDict(extra="forbid")


class AddMemoryRequest(_StrictModel):
    """``POST /v1/memories`` (PRD §10 ``add_memory``).

    ``kind`` and ``initiated_by`` are plain strings, validated by the memory
    service rather than by pydantic, so that a bad enum value produces the
    service's ``VALIDATION`` code and message on every surface — REST now, MCP
    later — instead of two different spellings of the same complaint.

    ``initiated_by`` defaults to ``agent``: the caller here *is* an agent, and a
    claim of "the user asked for this" should be made explicitly or not at all.
    """

    content: str
    kind: str = "fact"
    initiated_by: str = "agent"


class UpdateMemoryRequest(_StrictModel):
    """``PATCH /v1/memories/{id}`` — supersedes, never mutates (PRD §8.2)."""

    content: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    """The one error shape (PRD §10)."""
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _limit_write(limiter: RateLimiter | None, ctx: RequestContext) -> None:
    """Charge one write against this connection's budget (PRD §13, C2.10).

    Called after the scope check and before the service write, so a limited
    caller never touches the database. ``limiter is None`` disables limiting.
    """
    if limiter is None:
        return
    try:
        limiter.check(ctx.connection_id)
    except RateLimitExceeded as exc:
        raise RateLimitedError(str(exc), retry_after=exc.retry_after) from exc


def _bearer_token(request: Request) -> str:
    """Extract the bearer token, or raise ``UnauthorizedError``.

    Scheme comparison is case-insensitive because RFC 7235 says the scheme is;
    clients in the wild send ``bearer`` and ``BEARER``. The token itself is not
    touched — validating its shape is the auth layer's call, and guessing here
    would mean two places to change when C2 adds a format.
    """
    header = request.headers.get("Authorization")
    if not header:
        raise UnauthorizedError("missing Authorization header")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Authorization header must be 'Bearer <token>'")
    return token.strip()


def create_app(
    session_factory: Callable[[], Session],
    engine: MemoryEngine,
    authenticate: Authenticate,
    *,
    require_scope: RequireScope = _allow_every_scope,
    limiter: RateLimiter | None = None,
    title: str = "Purse",
) -> FastAPI:
    """Build the REST app.

    :param session_factory: Called once per request. The returned session is
        committed on success and rolled back on any exception, so a failed audit
        write can never leave an orphaned memory row.
    :param engine: The derived index (:class:`~purse.memory.engine.NullEngine`
        for M1). Shared across requests — implementations must be thread-safe.
    :param authenticate: ``(session, token) -> GatewayContext``. Raises
        :class:`~purse.gateway.errors.UnauthorizedError` for a bad token.
    :param require_scope: ``(ctx, scope) -> None``. Raises
        :class:`~purse.gateway.errors.ScopeError` when the scope is missing.
        Defaults to permitting everything — see the module docstring for why,
        and wire the real one from ``purse.auth``.
    :param limiter: The shared per-connection write limiter (PRD §13, C2.10).
        Only write endpoints are charged; reads are never limited. ``None``
        disables limiting — the default, so the M1 tests are unaffected. The
        assembled app passes ONE instance shared with the MCP surface
        (:mod:`purse.gateway.asgi`) so the budget is unified across both.
    """
    app = FastAPI(title=title, version="0.0.1")

    # -- request plumbing ---------------------------------------------------

    def db_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def caller(
        request: Request, session: Annotated[Session, Depends(db_session)]
    ) -> GatewayContext:
        return authenticate(session, _bearer_token(request))

    def request_context(
        request: Request, verified: Annotated[GatewayContext, Depends(caller)]
    ) -> RequestContext:
        """Bind the verified connection to this request's agent claim."""
        return RequestContext(caller=verified, agent_id=_agent_claim(request))

    # FastAPI caches a dependency per request, so an endpoint asking for both
    # `Ctx` and `Db` gets the same session the authenticator used — one session,
    # one transaction, one commit.
    Ctx = Annotated[RequestContext, Depends(request_context)]
    Db = Annotated[Session, Depends(db_session)]

    # -- error handling -----------------------------------------------------
    #
    # Handlers, not per-endpoint try/except: the shape in PRD §10 is a property
    # of the whole surface, and an endpoint added later gets it for free.

    # Handlers take `Exception` because that is Starlette's handler signature;
    # each is registered against a specific class, so the cast is safe.

    async def on_gateway_error(request: Request, exc: Exception) -> JSONResponse:
        error = cast(GatewayError, exc)
        response = _error_response(error.status, error.code, error.message)
        if error.status == 401:
            # RFC 6750: a 401 without this is a protocol violation, and some
            # clients use it to decide whether to start an OAuth flow (C2).
            response.headers["WWW-Authenticate"] = "Bearer"
        retry_after = getattr(error, "retry_after", None)
        if error.status == 429 and retry_after is not None:
            # RFC 6585: a 429 SHOULD say how long to wait. Seconds, rounded up so
            # a client that waits exactly this long finds the bucket has refilled.
            response.headers["Retry-After"] = str(math.ceil(retry_after))
        return response

    async def on_memory_error(request: Request, exc: Exception) -> JSONResponse:
        error = cast(MemoryError_, exc)
        return _error_response(_STATUS_BY_CODE.get(error.code, 400), error.code, error.message)

    async def on_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
        # pydantic's own error body is a different shape from PRD §10's, and a
        # client should not have to parse two.
        error = cast(RequestValidationError, exc)
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}"
            for item in error.errors()
        )
        return _error_response(422, "VALIDATION", detail or "invalid request")

    app.add_exception_handler(GatewayError, on_gateway_error)
    app.add_exception_handler(MemoryError_, on_memory_error)
    app.add_exception_handler(RequestValidationError, on_request_validation_error)

    # -- endpoints ----------------------------------------------------------

    @app.post("/v1/memories", status_code=201)
    def add_memory(body: AddMemoryRequest, ctx: Ctx, session: Db) -> dict[str, Any]:
        require_scope(ctx.caller, MEMORY_WRITE)
        _limit_write(limiter, ctx)
        record = service.add_memory(
            session,
            ctx,
            engine,
            content=body.content,
            kind=body.kind,
            initiated_by=body.initiated_by,
        )
        return record.as_dict()

    @app.get("/v1/memories")
    def list_memories(
        ctx: Ctx,
        session: Db,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = service.DEFAULT_LIST_LIMIT,
    ) -> dict[str, Any]:
        require_scope(ctx.caller, MEMORY_READ)
        page = service.list_memories(session, ctx, cursor=cursor, limit=limit)
        return {
            "items": [record.as_dict() for record in page.items],
            "next_cursor": page.next_cursor,
        }

    # Registered before `/v1/memories/{memory_id}` would be, so "search" is never
    # parsed as a uuid path parameter. FastAPI matches in declaration order.
    @app.get("/v1/memories/search")
    def search_memories(
        ctx: Ctx,
        session: Db,
        q: Annotated[str, Query()] = "",
        limit: Annotated[int, Query()] = service.DEFAULT_SEARCH_LIMIT,
    ) -> dict[str, Any]:
        require_scope(ctx.caller, MEMORY_READ)
        hits = service.search_memory(session, ctx, engine, query=q, limit=limit)
        return {"results": [hit.as_dict() for hit in hits]}

    @app.patch("/v1/memories/{memory_id}")
    def update_memory(
        memory_id: uuid.UUID, body: UpdateMemoryRequest, ctx: Ctx, session: Db
    ) -> dict[str, Any]:
        require_scope(ctx.caller, MEMORY_WRITE)
        _limit_write(limiter, ctx)
        record = service.update_memory(
            session, ctx, engine, memory_id=memory_id, content=body.content
        )
        return record.as_dict()

    @app.delete("/v1/memories/{memory_id}")
    def delete_memory(memory_id: uuid.UUID, ctx: Ctx, session: Db) -> dict[str, Any]:
        require_scope(ctx.caller, MEMORY_WRITE)
        _limit_write(limiter, ctx)
        service.delete_memory(session, ctx, memory_id=memory_id)
        # 200 with a body, not 204: the tombstone is idempotent, and echoing the
        # id back is what makes a retry legible in a client's logs.
        return {"id": str(memory_id), "deleted": True}

    return app
