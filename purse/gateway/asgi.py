"""The one Purse ASGI app: MCP + OAuth AS + REST on a single port (M2, PRD §12).

Topology — the FastMCP HTTP app is the root application, because that is where
the OAuth authorization server mounts its discovery and flow routes
(``/.well-known/*``, ``/authorize``, ``/token``, ``/register``, ``/revoke``, and
the standalone consent page). The MCP Streamable-HTTP endpoint sits at ``/mcp``;
the pre-MCP REST smoke surface (C3.8) is mounted under ``/v1``. All three share
one database engine, one memory engine, and — for MCP — one combined auth object
that accepts all six modes (OAuth in every flavour, plus ``purse_pat_`` bearers).

``create_purse_app_from_env`` is the deploy entrypoint (Fly/compose serve it with
uvicorn); ``create_purse_app`` is the explicit form the tests drive.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from sqlalchemy.engine import Engine
from starlette.applications import Starlette
from starlette.routing import Mount

from purse.auth.oauth import (
    PURSE_OAUTH_SECRET_ENV,
    PURSE_PUBLIC_URL_ENV,
    StaticClient,
    build_purse_auth,
)
from purse.db.session import create_db_engine, session_factory, session_scope
from purse.gateway.app import create_default_app
from purse.gateway.mcp import create_mcp_http_app, create_mcp_server
from purse.gateway.ratelimit import RateLimiter
from purse.memory.engine import MemoryEngine
from purse.memory.mem0_engine import build_memory_engine_from_env

__all__ = ["create_purse_app", "create_purse_app_from_env"]


def create_purse_app(
    *,
    public_url: str,
    secret: str,
    database_url: str | None = None,
    engine: Engine | None = None,
    memory_engine: MemoryEngine | None = None,
    static_clients: Sequence[StaticClient] = (),
    mcp_path: str = "/mcp",
    limiter: RateLimiter | None = None,
) -> Starlette:
    """Assemble the whole gateway.

    *public_url* is the externally reachable base URL — it is what the OAuth
    discovery metadata advertises as the issuer, so it must match how clients
    reach the server (behind a tunnel or proxy, the public hostname, not
    ``localhost``). *secret* signs the consent flow's pending-authorization
    tokens. *engine*/*memory_engine* are injectable for tests; production passes
    neither and gets a Postgres engine from *database_url*/``DATABASE_URL`` and a
    memory engine chosen by :func:`~purse.memory.mem0_engine.build_memory_engine_from_env`
    (a Mem0 adapter when ``PURSE_EMBEDDING_API_KEY`` is set, else a
    :class:`NullEngine`). *limiter* is the
    shared per-connection write limiter (PRD §13, C2.10); production passes none
    and gets the default 60/min limiter, while a test may inject a small one to
    prove the MCP and REST surfaces share one budget.
    """
    db_engine = engine if engine is not None else create_db_engine(database_url)
    make_session = session_factory(db_engine)
    # The Mem0 adapter (C3.4) when an embedding key is configured, else a
    # NullEngine — so staging without a key keeps the canonical + ILIKE path.
    # ``build_memory_engine_from_env`` reads the resolved DB url so its index
    # points at the same Postgres as the gateway.
    index = (
        memory_engine
        if memory_engine is not None
        else build_memory_engine_from_env(database_url=database_url)
    )

    # ONE limiter, shared by both surfaces (PRD §13, C2.10): the token bucket is
    # keyed by connection_id, so a connection's 60 writes/min budget is the same
    # whether it writes over MCP or REST. A test may inject a small one to prove
    # the shared budget; production builds the default 60/min limiter.
    write_limiter = limiter if limiter is not None else RateLimiter()

    auth = build_purse_auth(
        base_url=public_url,
        session_scope_factory=lambda: session_scope(db_engine),
        secret=secret,
        static_clients=static_clients,
    )

    server = create_mcp_server(
        session_factory=make_session, engine=index, auth=auth, limiter=write_limiter
    )
    # The MCP HTTP app is a Starlette app carrying the OAuth AS routes at its root
    # plus the streamable MCP endpoint at *mcp_path*, and — critically — its own
    # lifespan (the stateless session manager), which uvicorn/TestClient must run.
    app: Starlette = create_mcp_http_app(server, path=mcp_path)

    # The REST smoke surface authenticates PATs only (create_default_app wires the
    # PAT path); it shares the same engine and sessions. It already owns the full
    # ``/v1/...`` paths and its own FastAPI error handlers, so it is mounted with an
    # empty prefix (nothing stripped) and appended LAST — the MCP and OAuth routes
    # above match first, and only what they don't claim falls through to REST.
    rest_app = create_default_app(make_session=make_session, engine=index, limiter=write_limiter)
    app.router.routes.append(Mount("", app=rest_app))

    return app


def create_purse_app_from_env(
    *,
    env: Mapping[str, str] | None = None,
    memory_engine: MemoryEngine | None = None,
    static_clients: Sequence[StaticClient] = (),
) -> Starlette:
    """Deploy entrypoint: read ``PURSE_PUBLIC_URL`` + ``PURSE_OAUTH_SECRET`` (+ ``DATABASE_URL``)."""
    source = os.environ if env is None else env
    public_url = (source.get(PURSE_PUBLIC_URL_ENV) or "").strip()
    if not public_url:
        raise ValueError(f"{PURSE_PUBLIC_URL_ENV} must be set to the server's public base URL")
    secret = (source.get(PURSE_OAUTH_SECRET_ENV) or "").strip()
    if not secret:
        raise ValueError(f"{PURSE_OAUTH_SECRET_ENV} must be set to a signing secret")
    return create_purse_app(
        public_url=public_url,
        secret=secret,
        database_url=source.get("DATABASE_URL"),
        memory_engine=memory_engine,
        static_clients=static_clients,
    )
