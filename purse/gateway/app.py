"""Wiring: the REST gateway composed with real PAT auth (C2 + C3.8, PRD §12 M1).

:mod:`purse.gateway.rest` never imports :mod:`purse.auth`; the two are joined
here, and only here. The adapters translate the auth module's exceptions —
which carry an :class:`~purse.auth.errors.ErrorCode` but deliberately no HTTP
status — into the gateway's transport-level errors, keeping the status-code
table in the gateway where it belongs.

``create_default_app`` is what a server process will eventually serve (C4/C9);
until then it is the app the integration tests exercise end to end:
mint a PAT → bearer request → canonical write → audit row.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from sqlalchemy.orm import Session

from purse.auth.context import AuthContext
from purse.auth.context import require_scope as auth_require_scope
from purse.auth.errors import AuthenticationError
from purse.auth.errors import ScopeError as AuthScopeError
from purse.auth.pat import authenticate_pat
from purse.auth.scopes import Scope
from purse.db.session import create_db_engine, session_factory
from purse.gateway.errors import ScopeError, UnauthorizedError
from purse.gateway.rest import GatewayContext, create_app
from purse.memory.engine import MemoryEngine, NullEngine

__all__ = ["authenticate", "create_default_app", "require_scope"]


def authenticate(session: Session, token: str) -> AuthContext:
    """Resolve a bearer token to its connection, as a gateway hook.

    Every auth failure mode surfaces as the same 401 — ``AuthenticationError``
    is message-less by construction, so nothing here can widen it into an
    oracle.
    """
    try:
        return authenticate_pat(session, token)
    except AuthenticationError as exc:
        raise UnauthorizedError(str(exc)) from exc


def require_scope(ctx: GatewayContext, scope: str) -> None:
    """Enforce a scope string against the authenticated connection.

    ``Scope(scope)`` raises loudly on a scope the gateway invented and
    :mod:`purse.auth` does not know about — the right failure. The ``ctx`` a
    wired app passes in is always the :class:`AuthContext` that
    :func:`authenticate` returned.
    """
    if not isinstance(ctx, AuthContext):  # pragma: no cover - wiring invariant
        raise TypeError("wired require_scope expects the AuthContext from authenticate")
    try:
        auth_require_scope(ctx, Scope(scope))
    except AuthScopeError as exc:
        raise ScopeError(str(exc)) from exc


def create_default_app(
    *,
    database_url: str | None = None,
    engine: MemoryEngine | None = None,
    make_session: Callable[[], Session] | None = None,
) -> FastAPI:
    """The fully wired app: real DB sessions, real PAT auth, injectable engine.

    ``make_session`` exists for tests that must keep requests inside an outer
    rollback transaction; production callers pass nothing and get sessions from
    ``DATABASE_URL``. The memory engine defaults to :class:`NullEngine` until
    the Mem0 adapter lands (C3.4).
    """
    if make_session is None:
        make_session = session_factory(create_db_engine(database_url))
    return create_app(
        session_factory=make_session,
        engine=engine if engine is not None else NullEngine(),
        authenticate=authenticate,
        require_scope=require_scope,
    )
