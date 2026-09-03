"""Unit tests for the operator session layer (C7) — no database.

These prove the token scheme and the password gate in isolation: sign/verify,
tamper and expiry rejection, the wrong-password path, and login being disabled
when the environment is unconfigured. Every one of these must hold with Postgres
down, so none of them touch a session.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from purse.memory.engine import NullEngine
from purse.web.app import create_web_router
from purse.web.errors import InvalidCredentialsError, LoginDisabledError, UnauthenticatedError
from purse.web.session import SessionContext, SessionManager

SECRET = "unit-test-session-secret"  # noqa: S105 - a fixture constant, not a real secret
PASSWORD = "correct horse battery staple"  # noqa: S105 - a fixture constant


def _manager(**kwargs: object) -> SessionManager:
    params: dict[str, object] = {"secret": SECRET, "password": PASSWORD}
    params.update(kwargs)
    return SessionManager(**params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Token sign / verify / expiry / tamper
# ---------------------------------------------------------------------------


def test_issue_then_verify_roundtrips_the_operator_identity() -> None:
    manager = _manager()
    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()

    token = manager.issue_token(user_id=user_id, workspace_id=workspace_id)
    ctx = manager.verify_token(token)

    assert isinstance(ctx, SessionContext)
    assert ctx.user_id == user_id
    assert ctx.workspace_id == workspace_id


def test_verify_rejects_a_tampered_token() -> None:
    manager = _manager()
    token = manager.issue_token(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())

    with pytest.raises(UnauthenticatedError):
        manager.verify_token(token + "x")


def test_verify_rejects_a_token_signed_with_another_secret() -> None:
    token = _manager().issue_token(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())

    other_secret = "a-different-secret"  # noqa: S105 - a fixture constant, not a real secret
    with pytest.raises(UnauthenticatedError):
        _manager(secret=other_secret).verify_token(token)


def test_verify_rejects_an_expired_token() -> None:
    # A manager with a negative TTL treats any already-issued token as too old —
    # deterministic expiry with no sleep.
    manager = _manager()
    token = manager.issue_token(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())

    with pytest.raises(UnauthenticatedError):
        _manager(ttl_seconds=-1).verify_token(token)


def test_verify_rejects_garbage() -> None:
    manager = _manager()
    for garbage in ("", "not-a-token", "a.b.c"):
        with pytest.raises(UnauthenticatedError):
            manager.verify_token(garbage)


def test_verify_is_disabled_without_a_secret() -> None:
    # No signing secret → every token is unverifiable, never a server error.
    manager = SessionManager(secret=None, password=PASSWORD)
    with pytest.raises(UnauthenticatedError):
        manager.verify_token("anything")


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------


def test_verify_password_accepts_the_configured_password() -> None:
    _manager().verify_password(PASSWORD)  # does not raise


def test_verify_password_rejects_the_wrong_password() -> None:
    manager = _manager()
    with pytest.raises(InvalidCredentialsError):
        manager.verify_password("wrong")


def test_verify_password_rejects_a_wrong_password_of_equal_length() -> None:
    # Same length as PASSWORD, so the rejection is the compare_digest result,
    # not a length short-circuit — the timing shape is the same as any miss.
    manager = _manager()
    same_length = "x" * len(PASSWORD)
    assert len(same_length) == len(PASSWORD)
    with pytest.raises(InvalidCredentialsError):
        manager.verify_password(same_length)


def test_login_is_disabled_when_password_is_unset() -> None:
    manager = SessionManager(secret=SECRET, password=None)
    assert manager.login_enabled is False
    with pytest.raises(LoginDisabledError):
        manager.verify_password("anything")


def test_login_is_disabled_when_secret_is_unset() -> None:
    manager = SessionManager(secret=None, password=PASSWORD)
    assert manager.login_enabled is False
    with pytest.raises(LoginDisabledError):
        manager.verify_password(PASSWORD)


def test_login_enabled_only_when_both_present() -> None:
    assert _manager().login_enabled is True


def test_from_env_reads_both_variables() -> None:
    manager = SessionManager.from_env(
        {"PURSE_OWNER_PASSWORD": PASSWORD, "PURSE_SESSION_SECRET": SECRET}
    )
    assert manager.login_enabled is True
    manager.verify_password(PASSWORD)


# ---------------------------------------------------------------------------
# require_session dependency through the app (no DB reached)
# ---------------------------------------------------------------------------


@pytest.fixture
def unconfigured_client() -> TestClient:
    app = create_web_router(
        Session, NullEngine(), sessions=SessionManager(secret=SECRET, password=None)
    )
    return TestClient(app)


@pytest.fixture
def configured_client() -> TestClient:
    app = create_web_router(Session, NullEngine(), sessions=_manager())
    return TestClient(app)


def test_login_returns_login_disabled_when_unconfigured(unconfigured_client: TestClient) -> None:
    response = unconfigured_client.post("/login", json={"password": "anything"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LOGIN_DISABLED"


def test_protected_route_without_a_token_is_401(configured_client: TestClient) -> None:
    response = configured_client.get("/session")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_protected_route_with_garbage_token_is_401(configured_client: TestClient) -> None:
    response = configured_client.get(
        "/session", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_protected_route_with_non_bearer_scheme_is_401(configured_client: TestClient) -> None:
    response = configured_client.get("/session", headers={"Authorization": "Basic abc"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
