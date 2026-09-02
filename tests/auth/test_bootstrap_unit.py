"""Bootstrap pieces that need no database: config resolution and the printed block."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from purse.auth.bootstrap import (
    DEFAULT_USER_EMAIL,
    ONBOARDING_CLIENT_NAME,
    PERSONAL_WORKSPACE_NAME,
    USER_EMAIL_ENV,
    BootstrapResult,
    format_credentials,
    owner_email,
)
from purse.auth.scopes import ONBOARDING_SCOPES
from purse.auth.tokens import TOKEN_PREFIX, generate_token


def test_owner_email_defaults_to_the_self_host_owner() -> None:
    assert owner_email({}) == DEFAULT_USER_EMAIL
    assert owner_email({USER_EMAIL_ENV: "   "}) == DEFAULT_USER_EMAIL
    assert DEFAULT_USER_EMAIL == "owner@localhost"


def test_owner_email_reads_and_strips_the_environment_value() -> None:
    assert owner_email({USER_EMAIL_ENV: "  me@example.test "}) == "me@example.test"


def test_the_default_workspace_is_personal() -> None:
    assert PERSONAL_WORKSPACE_NAME == "Personal"


def _result() -> BootstrapResult:
    return BootstrapResult(
        user_id=uuid.uuid4(),
        email="owner@localhost",
        workspace_id=uuid.uuid4(),
        workspace_name=PERSONAL_WORKSPACE_NAME,
        connection_id=uuid.uuid4(),
        token=generate_token(),
        user_created=True,
        workspace_created=True,
    )


def test_credentials_block_carries_everything_a_first_boot_needs() -> None:
    result = _result()
    block = format_credentials(result)
    assert result.token.reveal() in block
    assert TOKEN_PREFIX in block
    assert "shown once" in block
    assert str(result.workspace_id) in block
    assert str(result.connection_id) in block
    assert ONBOARDING_CLIENT_NAME in block
    assert result.email in block
    for scope in ONBOARDING_SCOPES:
        assert scope.value in block


def test_bootstrap_result_is_immutable() -> None:
    result = _result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.email = "someone@else"  # type: ignore[misc]


def test_repr_of_the_result_does_not_leak_the_token() -> None:
    """A dataclass repr is exactly the thing that ends up in a traceback."""
    result = _result()
    rendered = repr(result)
    assert result.token.reveal() not in rendered
    assert result.token.reveal().removeprefix(TOKEN_PREFIX)[4:] not in rendered
