"""Fixtures for the database tests.

Everything in ``tests/db`` is marked ``db`` and needs a real Postgres; see
``tests/conftest.py`` for how one is found (or why the tests skip).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy.orm import Session

from purse.db.models import AuthMode, Connection, User
from purse.db.repo import Repo, create_user, create_workspace

pytestmark = pytest.mark.db


@dataclass(frozen=True)
class TwoWorkspaces:
    """Two workspaces in one vault, each with its own connection.

    Every isolation test needs exactly this shape: somewhere to write, and
    somewhere else that must not see it.
    """

    user: User
    alpha: Repo
    beta: Repo
    alpha_connection: Connection
    beta_connection: Connection


@pytest.fixture
def session(db_session: Session) -> Session:
    return db_session


@pytest.fixture
def user(session: Session) -> User:
    return create_user(session, email=f"user-{uuid.uuid4().hex[:10]}@example.test")


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
def two_workspaces(session: Session, user: User) -> Iterator[TwoWorkspaces]:
    alpha_ws = create_workspace(session, user_id=user.id, name="Personal")
    beta_ws = create_workspace(session, user_id=user.id, name="Work")
    alpha = Repo.open(session, alpha_ws.id)
    beta = Repo.open(session, beta_ws.id)
    yield TwoWorkspaces(
        user=user,
        alpha=alpha,
        beta=beta,
        alpha_connection=alpha.add_connection(
            client_name="cursor", auth_mode=AuthMode.OAUTH_STATIC, writes_enabled=True
        ),
        beta_connection=beta.add_connection(
            client_name="codex", auth_mode=AuthMode.PAT, writes_enabled=True
        ),
    )
