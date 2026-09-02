"""Fixtures for the skills service tests (C5).

The db-marked tests mirror ``tests/memory/conftest.py``: a throwaway migrated
database from ``tests/conftest.py``, a workspace, a connection, and a transaction
rolled back after each test, plus a :class:`StubContext` standing in for the
authenticated caller ``purse.auth`` supplies. A second workspace fixture backs the
isolation test.
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
    return create_user(session, email=f"skills-{uuid.uuid4().hex[:10]}@example.test")


@pytest.fixture
def repo(session: Session, user: User) -> Repo:
    workspace = create_workspace(session, user_id=user.id, name="Personal")
    return Repo.open(session, workspace.id)


@pytest.fixture
def connection(repo: Repo) -> Connection:
    return repo.add_connection(
        client_name="claude-code",
        auth_mode=AuthMode.PAT,
        scopes=["skills:read", "skills:write"],
        writes_enabled=True,
    )


@pytest.fixture
def ctx(repo: Repo, connection: Connection) -> StubContext:
    """The authenticated caller for a real workspace + connection."""
    return StubContext(
        connection_id=connection.id,
        workspace_id=repo.workspace_id,
        scopes=("skills:read", "skills:write"),
    )


@pytest.fixture
def other_ctx(session: Session, user: User) -> StubContext:
    """A second workspace in the *same* vault, for isolation tests."""
    workspace = create_workspace(session, user_id=user.id, name="Second")
    repo = Repo.open(session, workspace.id)
    connection = repo.add_connection(
        client_name="claude-code",
        auth_mode=AuthMode.PAT,
        scopes=["skills:read", "skills:write"],
        writes_enabled=True,
    )
    return StubContext(
        connection_id=connection.id,
        workspace_id=workspace.id,
        scopes=("skills:read", "skills:write"),
    )
