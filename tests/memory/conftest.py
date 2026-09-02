"""Fixtures for the memory service tests (C3).

The db-marked tests here mirror ``tests/db/conftest.py``: a throwaway migrated
database from ``tests/conftest.py``, a workspace, a connection, and a
transaction rolled back after each test. What they add is the piece the memory
service needs and the data layer does not — a :class:`StubContext` standing in
for the authenticated caller ``purse.auth`` will supply.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from purse.db.models import AuthMode, Connection, User
from purse.db.repo import Repo, create_user, create_workspace
from tests.conftest import StubContext


@pytest.fixture
def session(db_session: Session) -> Session:
    return db_session


@pytest.fixture
def user(session: Session) -> User:
    return create_user(session, email=f"memory-{uuid.uuid4().hex[:10]}@example.test")


@pytest.fixture
def repo(session: Session, user: User) -> Repo:
    workspace = create_workspace(session, user_id=user.id, name="Personal")
    return Repo.open(session, workspace.id)


@pytest.fixture
def connection(repo: Repo) -> Connection:
    return repo.add_connection(
        client_name="claude-code",
        auth_mode=AuthMode.PAT,
        scopes=["memory:read", "memory:write"],
        writes_enabled=True,
    )


@pytest.fixture
def ctx(repo: Repo, connection: Connection) -> StubContext:
    """The authenticated caller for a real workspace + connection."""
    return StubContext(
        connection_id=connection.id,
        workspace_id=repo.workspace_id,
        agent_id="claude-code/1.0",
    )
