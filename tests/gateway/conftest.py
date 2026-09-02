"""Fixtures for the REST gateway tests (C3.8).

Two flavours of app, because two different things are being tested.

``client``
    No database at all. The session factory hands out an unbound session and a
    fake ``authenticate`` decides the outcome. This is where bearer parsing,
    error shapes, and status codes are proved — none of which need a row to
    exist, and all of which should keep working when Postgres is down.

``db_gateway``
    A real migrated Postgres and the real service, for CRUD. Setup rows and the
    app's own request-scoped sessions share **one connection inside one outer
    transaction**, with ``join_transaction_mode="create_savepoint"``. That is
    what lets the app ``commit`` per request exactly as it does in production
    (each commit releases a savepoint) while the whole test still rolls back at
    the end. The alternative — committing setup data for real and deleting it
    afterwards — is not available here: ``memories`` has a trigger that rejects
    ``DELETE``, so a cascading cleanup would fail on any test that wrote one.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from purse.db.models import AuthMode
from purse.db.repo import Repo, create_user, create_workspace
from purse.gateway.errors import ScopeError, UnauthorizedError
from purse.gateway.rest import GatewayContext, create_app
from purse.memory.engine import MemoryEngine, NullEngine
from tests.conftest import StubContext

#: The only token the fake authenticator accepts.
GOOD_TOKEN = "purse_pat_test_token"  # noqa: S105 - a fixture constant, not a credential


def fake_authenticate(ctx: GatewayContext) -> Callable[[Session, str], GatewayContext]:
    """An ``authenticate`` that accepts exactly one token and returns *ctx*.

    Stands in for C2 without importing it, which is the whole point of
    ``authenticate`` being injected: the auth work lands in parallel and
    replaces this at the wiring site, with no change to the gateway.
    """

    def authenticate(session: Session, token: str) -> GatewayContext:
        if token != GOOD_TOKEN:
            raise UnauthorizedError("invalid token")
        return ctx

    return authenticate


def scope_enforcer(granted: set[str]) -> Callable[[GatewayContext, str], None]:
    """A stand-in for C2.2's middleware: raises ``ScopeError`` for anything absent."""

    def require_scope(ctx: GatewayContext, scope: str) -> None:
        if scope not in granted:
            raise ScopeError(f"this connection lacks the {scope} scope")

    return require_scope


# ---------------------------------------------------------------------------
# Database-free app
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ctx() -> StubContext:
    return StubContext(connection_id=uuid.uuid4(), workspace_id=uuid.uuid4(), agent_id="pytest")


@pytest.fixture
def client(fake_ctx: StubContext) -> Iterator[TestClient]:
    """An app whose sessions are bound to nothing.

    Safe because every test using it fails (401/403/422) before the service is
    reached. One that ever gets further raises instead of quietly passing, which
    is the right failure mode.
    """
    app = create_app(
        session_factory=Session,
        engine=NullEngine(),
        authenticate=fake_authenticate(fake_ctx),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def scoped_client(fake_ctx: StubContext) -> Iterator[Callable[[set[str]], TestClient]]:
    """Build database-free clients with a given set of granted scopes.

    Denying a scope is also the cleanest way to prove that *authentication*
    succeeded without a database: a 403 can only be reached by a request whose
    token resolved to a caller.
    """
    built: list[TestClient] = []

    def build(granted: set[str]) -> TestClient:
        app = create_app(
            session_factory=Session,
            engine=NullEngine(),
            authenticate=fake_authenticate(fake_ctx),
            require_scope=scope_enforcer(granted),
        )
        test_client = TestClient(app)
        built.append(test_client)
        return test_client

    try:
        yield build
    finally:
        for test_client in built:
            test_client.close()


# ---------------------------------------------------------------------------
# Database-backed app
# ---------------------------------------------------------------------------


@dataclass
class DbGateway:
    """A real workspace, plus the two things a test needs to poke at it.

    ``session_factory`` is the *same* factory the app uses, so a test can read
    the audit log from inside the same transaction the requests wrote in.
    """

    ctx: StubContext
    session_factory: Callable[[], Session]
    build: Callable[..., TestClient]

    def client(
        self, *, engine: MemoryEngine | None = None, granted: set[str] | None = None
    ) -> TestClient:
        return self.build(engine=engine, granted=granted)


@pytest.fixture
def db_gateway(migrated_engine: Engine) -> Iterator[DbGateway]:
    connection = migrated_engine.connect()
    transaction = connection.begin()

    def make_session() -> Session:
        return Session(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    with make_session() as setup:
        user = create_user(setup, email=f"gateway-{uuid.uuid4().hex[:10]}@example.test")
        workspace = create_workspace(setup, user_id=user.id, name="Personal")
        repo = Repo.open(setup, workspace.id)
        connection_row = repo.add_connection(
            client_name="curl",
            auth_mode=AuthMode.PAT,
            scopes=["memory:read", "memory:write"],
            writes_enabled=True,
        )
        ctx = StubContext(
            connection_id=connection_row.id, workspace_id=workspace.id, agent_id="curl/8"
        )
        # Releases the savepoint, so the app's own sessions on this same
        # connection can see the workspace. The outer transaction still holds.
        setup.commit()

    def build(*, engine: MemoryEngine | None = None, granted: set[str] | None = None) -> TestClient:
        index = engine if engine is not None else NullEngine()
        authenticate = fake_authenticate(ctx)
        # `granted=None` builds the app the way it is built before C2 lands:
        # `require_scope` not passed at all, i.e. the permissive default.
        if granted is None:
            app = create_app(session_factory=make_session, engine=index, authenticate=authenticate)
        else:
            app = create_app(
                session_factory=make_session,
                engine=index,
                authenticate=authenticate,
                require_scope=scope_enforcer(granted),
            )
        return TestClient(app)

    try:
        yield DbGateway(ctx=ctx, session_factory=make_session, build=build)
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def db_client(db_gateway: DbGateway) -> Iterator[TestClient]:
    with db_gateway.client() as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {GOOD_TOKEN}"}


# ---------------------------------------------------------------------------
# MCP gateway (C4)
# ---------------------------------------------------------------------------
#
# Appended for the MCP surface. The MCP tools read the caller's identity from a
# *verified token* (FastMCP's `get_access_token()`), not from an injected
# `authenticate` hook — so the stand-in for C2 here is a `TokenVerifier` that
# returns chosen claims for a chosen token, exactly as the parallel OAuth agent's
# real provider will. Nothing imports the OAuth server.

from fastmcp import FastMCP  # noqa: E402
from fastmcp.server.auth import AccessToken, TokenVerifier  # noqa: E402

from purse.auth.oauth.claims import build_access_token  # noqa: E402
from purse.gateway.mcp import create_mcp_server  # noqa: E402

#: The one bearer token the fake verifier accepts over the HTTP transport.
MCP_GOOD_TOKEN = "purse_mcp_test_token"  # noqa: S105 - a fixture constant, not a credential


class FakeMCPVerifier(TokenVerifier):
    """A `TokenVerifier` that mints one connection's verified claims for one token.

    Stands in for the OAuth agent's `OAuthProvider` without importing it: the MCP
    server only needs *an* `AuthProvider` whose `verify_token` yields an
    `AccessToken` whose `scopes` are the granted scopes and whose `claims` carry
    the Purse connection identity. That is the whole seam.
    """

    def __init__(
        self,
        *,
        connection_id: uuid.UUID,
        workspace_id: uuid.UUID,
        scopes: Collection[str] = ("memory:read", "memory:write"),
        writes_enabled: bool = True,
        client_name: str = "pytest-mcp",
        token: str = MCP_GOOD_TOKEN,
    ) -> None:
        super().__init__()
        self._token = token
        # Mint through the same builder the real OAuth provider and PAT verifier
        # use, so the fake exercises the exact claim shape (namespaced keys) the
        # tool layer reads — the seam is tested, not a parallel invention.
        self._access = build_access_token(
            token=token,
            connection_id=connection_id,
            workspace_id=workspace_id,
            scopes=[str(scope) for scope in scopes],
            writes_enabled=writes_enabled,
            client_name=client_name,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self._token:
            return None
        return self._access


@dataclass
class MCPDbVault:
    """A real workspace + connection, and a builder for MCP servers bound to it.

    Mirrors :class:`DbGateway` for the MCP surface: ``build`` returns a
    ``create_mcp_server`` whose fake verifier authenticates :data:`MCP_GOOD_TOKEN`
    as this connection, sharing the app's request sessions with the test's outer
    rollback transaction (savepoint-per-commit).
    """

    connection_id: uuid.UUID
    workspace_id: uuid.UUID
    session_factory: Callable[[], Session]
    build: Callable[..., FastMCP]


@pytest.fixture
def mcp_db(migrated_engine: Engine) -> Iterator[MCPDbVault]:
    connection = migrated_engine.connect()
    transaction = connection.begin()

    def make_session() -> Session:
        return Session(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    with make_session() as setup:
        user = create_user(setup, email=f"mcp-{uuid.uuid4().hex[:10]}@example.test")
        workspace = create_workspace(setup, user_id=user.id, name="Personal")
        repo = Repo.open(setup, workspace.id)
        connection_row = repo.add_connection(
            client_name="mcp-client",
            auth_mode=AuthMode.PAT,
            scopes=["memory:read", "memory:write"],
            writes_enabled=True,
        )
        connection_id = connection_row.id
        workspace_id = workspace.id
        setup.commit()

    def build(
        *,
        scopes: Collection[str] = ("memory:read", "memory:write"),
        writes_enabled: bool = True,
        engine: MemoryEngine | None = None,
    ) -> FastMCP:
        verifier = FakeMCPVerifier(
            connection_id=connection_id,
            workspace_id=workspace_id,
            scopes=scopes,
            writes_enabled=writes_enabled,
        )
        return create_mcp_server(
            session_factory=make_session,
            engine=engine if engine is not None else NullEngine(),
            auth=verifier,
        )

    try:
        yield MCPDbVault(
            connection_id=connection_id,
            workspace_id=workspace_id,
            session_factory=make_session,
            build=build,
        )
    finally:
        transaction.rollback()
        connection.close()
