"""First boot against a real database: idempotent identity, a fresh token every run."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from purse.auth.bootstrap import (
    ONBOARDING_CLIENT_NAME,
    PERSONAL_WORKSPACE_NAME,
    USER_EMAIL_ENV,
    bootstrap,
    ensure_user,
    ensure_workspace,
    format_credentials,
)
from purse.auth.pat import authenticate_pat
from purse.auth.scopes import ONBOARDING_SCOPES, Scope
from purse.auth.tokens import TOKEN_PREFIX
from purse.db.models import User
from purse.db.repo import Repo, create_user, list_user_workspaces
from purse.skills import service as skills_service
from tests.conftest import StubContext

pytestmark = pytest.mark.db

EMAIL = "bootstrap-owner@example.test"


# -- get-or-create -----------------------------------------------------------


def test_ensure_user_creates_then_reuses(session: Session) -> None:
    user, created = ensure_user(session, email=EMAIL)
    assert created is True
    again, created_again = ensure_user(session, email=EMAIL)
    assert created_again is False
    assert again.id == user.id
    assert len(list(session.scalars(select(User).where(User.email == EMAIL)))) == 1


def test_ensure_workspace_creates_then_reuses(session: Session) -> None:
    user = create_user(session, email=EMAIL)
    workspace, created = ensure_workspace(session, user_id=user.id)
    assert created is True
    assert workspace.name == PERSONAL_WORKSPACE_NAME
    again, created_again = ensure_workspace(session, user_id=user.id)
    assert created_again is False
    assert again.id == workspace.id
    assert len(list_user_workspaces(session, user.id)) == 1


# -- bootstrap ---------------------------------------------------------------


def test_first_boot_produces_a_usable_onboarding_credential(session: Session) -> None:
    result = bootstrap(session, email=EMAIL)

    assert result.user_created is True
    assert result.workspace_created is True
    assert result.email == EMAIL
    assert result.workspace_name == PERSONAL_WORKSPACE_NAME

    ctx = authenticate_pat(session, result.token.reveal())
    assert ctx.connection_id == result.connection_id
    assert ctx.workspace_id == result.workspace_id
    assert ctx.client_name == ONBOARDING_CLIENT_NAME
    # PRD §7.1: the onboarding connection is the one that can write.
    assert ctx.scopes == ONBOARDING_SCOPES
    assert ctx.writes_enabled is True
    assert ctx.has(Scope.MEMORY_WRITE)
    assert not ctx.has(Scope.SKILLS_WRITE)
    assert not ctx.has(Scope.APIS_USE)


def test_second_run_reuses_identity_and_mints_a_new_token(session: Session) -> None:
    """Idempotent identity; a fresh credential. The old one was never recoverable."""
    first = bootstrap(session, email=EMAIL)
    second = bootstrap(session, email=EMAIL)

    assert second.user_id == first.user_id
    assert second.workspace_id == first.workspace_id
    assert second.user_created is False
    assert second.workspace_created is False

    assert second.connection_id != first.connection_id
    assert second.token.reveal() != first.token.reveal()

    # Both stay valid until one is revoked — re-running is a recovery path, not
    # a rotation that silently locks out a working client.
    assert authenticate_pat(session, first.token.reveal()).connection_id == first.connection_id
    assert authenticate_pat(session, second.token.reveal()).connection_id == second.connection_id

    assert len(list_user_workspaces(session, first.user_id)) == 1
    assert len(list(session.scalars(select(User).where(User.email == EMAIL)))) == 1


def test_bootstrap_defaults_the_email_from_the_environment(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_EMAIL_ENV, "env-owner@example.test")
    assert bootstrap(session).email == "env-owner@example.test"


def test_bootstrap_workspaces_are_separate_when_named_separately(session: Session) -> None:
    personal = bootstrap(session, email=EMAIL)
    work = bootstrap(session, email=EMAIL, workspace_name="Work")
    assert work.workspace_id != personal.workspace_id
    assert authenticate_pat(session, work.token.reveal()).workspace_id == work.workspace_id


# -- the bundled skill (C5.4) -------------------------------------------------


def test_bootstrap_seeds_the_save_policy_skill(session: Session) -> None:
    """First boot must leave the workspace with a usable ``purse-save-policy``.

    PRD §8.2: the guided "what is worth saving" moment depends on this skill
    existing from the first request, not on a client upserting it later.
    """
    result = bootstrap(session, email=EMAIL)
    ctx = StubContext(connection_id=result.connection_id, workspace_id=result.workspace_id)

    skill = skills_service.get_skill(session, ctx, name="purse-save-policy")
    assert skill.version == "1.0.0"
    assert skill.description


def test_bootstrap_does_not_duplicate_the_seeded_skill_on_rerun(session: Session) -> None:
    """Re-running bootstrap (a lost-credential recovery) must stay a no-op for the
    seed: no second version, and no exception from the idempotent upsert path."""
    first = bootstrap(session, email=EMAIL)
    bootstrap(session, email=EMAIL)  # must not raise

    repo = Repo.open(session, first.workspace_id)
    versions = repo.list_skill_versions("purse-save-policy")
    assert len(versions) == 1


# -- the printed block -------------------------------------------------------


def test_credentials_block_shows_the_token_once_with_a_warning(session: Session) -> None:
    result = bootstrap(session, email=EMAIL)
    block = format_credentials(result)

    # This is the one place the raw token is meant to be visible.
    assert result.token.reveal() in block
    assert TOKEN_PREFIX in block
    assert "shown once" in block
    assert str(result.workspace_id) in block
    assert result.email in block
    for scope in ONBOARDING_SCOPES:
        assert scope.value in block


def test_credentials_block_does_not_leak_the_stored_hash(session: Session) -> None:
    """The digest is a database detail; printing it invites someone to treat it as a credential."""
    result = bootstrap(session, email=EMAIL)
    assert result.token.digest() not in format_credentials(result)
