"""Fixtures for the ``/web`` dashboard tests (C7).

Mirrors ``tests/gateway/conftest.py``'s db pattern: setup rows and the app's
own request-scoped sessions share **one connection inside one outer
transaction**, with ``join_transaction_mode="create_savepoint"``. The app
``commit``s per request exactly as in production (each commit releases a
savepoint) while the whole test still rolls back at the end — which matters here
because ``memories`` has a trigger that rejects ``DELETE``, so a cascading
cleanup is not an option.

The operator is a real ``users`` row with a ``Personal`` workspace, resolved the
same way production does (:func:`purse.web.session.resolve_operator`). The
fixture logs in through ``/web/login`` to obtain a genuine signed session token,
so every db test drives the real auth path end to end.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from purse.db.repo import Repo, create_user, create_workspace
from purse.memory.engine import MemoryEngine, NullEngine
from purse.web.app import create_web_router
from purse.web.session import SessionManager

SECRET = "web-db-test-session-secret"  # noqa: S105 - a fixture constant, not a real secret
PASSWORD = "web-db-test-operator-password"  # noqa: S105 - a fixture constant


@dataclass
class WebVault:
    """A migrated vault with one operator, plus a TestClient and a login helper."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    session_factory: Callable[[], Session]
    client: TestClient
    token: str

    def auth(self) -> dict[str, str]:
        """Authorization header carrying the operator's session token."""
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def web_vault(migrated_engine: Engine) -> Iterator[WebVault]:
    connection = migrated_engine.connect()
    transaction = connection.begin()

    def make_session() -> Session:
        return Session(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    with make_session() as setup:
        user = create_user(setup, email=f"web-{uuid.uuid4().hex[:10]}@example.test")
        workspace = create_workspace(setup, user_id=user.id, name="Personal")
        user_id = user.id
        workspace_id = workspace.id
        # Release the savepoint so the app's own sessions on this connection can
        # see the operator. The outer transaction still holds for teardown.
        setup.commit()

    engine: MemoryEngine = NullEngine()
    app = create_web_router(
        make_session,
        engine,
        sessions=SessionManager(secret=SECRET, password=PASSWORD),
    )
    client = TestClient(app)

    login = client.post("/login", json={"password": PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["session_token"]

    try:
        yield WebVault(
            user_id=user_id,
            workspace_id=workspace_id,
            session_factory=make_session,
            client=client,
            token=token,
        )
    finally:
        client.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def second_workspace(web_vault: WebVault) -> uuid.UUID:
    """A second, unrelated workspace with one memory — for the isolation test.

    Written through the same savepoint session so it is visible to the app's
    requests, but it belongs to a different user and workspace, so the operator's
    session must never surface its rows.
    """
    from purse.db.models import AuthMode, InitiatedBy, MemoryKind

    with web_vault.session_factory() as setup:
        other_user = create_user(setup, email=f"other-{uuid.uuid4().hex[:10]}@example.test")
        other_ws = create_workspace(setup, user_id=other_user.id, name="Personal")
        repo = Repo.open(setup, other_ws.id)
        conn = repo.add_connection(
            client_name="other-client",
            auth_mode=AuthMode.PAT,
            scopes=["memory:read", "memory:write"],
            writes_enabled=True,
        )
        repo.add_memory(
            content="a secret in another vault",
            kind=MemoryKind.FACT,
            connection_id=conn.id,
            initiated_by=InitiatedBy.AGENT,
        )
        workspace_id = other_ws.id
        setup.commit()
    return workspace_id
