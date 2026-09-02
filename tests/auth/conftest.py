"""Fixtures for the auth tests.

Deliberately a sibling of ``tests/db/conftest.py`` rather than an import of it:
the two suites happen to need the same shape of scaffolding today, and coupling
them would mean a change made for an isolation test silently rewrites the
premise of an authentication test.

Only the fixtures below touch a database. The unit tests in this directory take
none of them and run everywhere; see ``tests/conftest.py`` for how a Postgres is
found, and why its absence is a skip locally and a failure in CI.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.orm import Session

from purse.db.models import User, Workspace
from purse.db.repo import create_user, create_workspace


@dataclass(frozen=True)
class TwoWorkspaces:
    """One vault, two workspaces — the shape every cross-workspace test needs."""

    user: User
    alpha: Workspace
    beta: Workspace


@pytest.fixture
def session(db_session: Session) -> Session:
    return db_session


@pytest.fixture
def user(session: Session) -> User:
    return create_user(session, email=f"auth-{uuid.uuid4().hex[:10]}@example.test")


@pytest.fixture
def workspace(session: Session, user: User) -> Workspace:
    return create_workspace(session, user_id=user.id, name="Personal")


@pytest.fixture
def two_workspaces(session: Session, user: User) -> TwoWorkspaces:
    return TwoWorkspaces(
        user=user,
        alpha=create_workspace(session, user_id=user.id, name="Personal"),
        beta=create_workspace(session, user_id=user.id, name="Work"),
    )
