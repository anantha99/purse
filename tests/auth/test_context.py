"""C2.2: AuthContext is immutable, and require_scope is the only gate."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from purse.auth.context import AuthContext, require_scope
from purse.auth.errors import (
    AUTHENTICATION_FAILED_MESSAGE,
    AuthenticationError,
    AuthError,
    ErrorCode,
    ScopeError,
)
from purse.auth.scopes import ALL_SCOPES, Scope


def make_context(
    *,
    scopes: frozenset[Scope] = ALL_SCOPES,
    writes_enabled: bool = True,
    client_name: str = "claude-code",
) -> AuthContext:
    return AuthContext(
        connection_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        scopes=scopes,
        writes_enabled=writes_enabled,
        client_name=client_name,
    )


# -- immutability ------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connection_id", uuid.uuid4()),
        ("workspace_id", uuid.uuid4()),
        ("scopes", ALL_SCOPES),
        ("writes_enabled", True),
        ("client_name", "somebody-else"),
    ],
)
def test_context_fields_cannot_be_reassigned(field: str, value: object) -> None:
    ctx = make_context(scopes=frozenset(), writes_enabled=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(ctx, field, value)


def test_context_cannot_grow_new_attributes() -> None:
    """slots=True: no smuggling extra state onto a verified context.

    The exception type is not pinned. A frozen ``slots=True`` dataclass raises
    ``FrozenInstanceError`` for a declared field but, because ``slots=True``
    rebuilds the class after the frozen ``__setattr__`` closed over the
    original, currently raises ``TypeError`` for an undeclared one. What is
    being asserted is the refusal, not CPython's choice of how to spell it.
    """
    ctx = make_context()
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        ctx.workspace_id_override = uuid.uuid4()  # type: ignore[attr-defined]
    assert not hasattr(ctx, "workspace_id_override")


def test_scopes_are_a_frozenset_so_a_grant_cannot_be_widened_in_place() -> None:
    ctx = make_context(scopes=frozenset({Scope.MEMORY_READ}))
    assert isinstance(ctx.scopes, frozenset)
    assert not hasattr(ctx.scopes, "add")


def test_scope_strings_are_sorted() -> None:
    ctx = make_context(scopes=frozenset({Scope.SKILLS_READ, Scope.MEMORY_READ}))
    assert ctx.scope_strings() == ["memory:read", "skills:read"]


# -- require_scope -----------------------------------------------------------


def test_granted_read_scope_passes() -> None:
    ctx = make_context(scopes=frozenset({Scope.MEMORY_READ}), writes_enabled=False)
    require_scope(ctx, Scope.MEMORY_READ)


def test_missing_scope_is_refused_with_the_prd_error_code() -> None:
    ctx = make_context(scopes=frozenset({Scope.MEMORY_READ}))
    with pytest.raises(ScopeError) as caught:
        require_scope(ctx, Scope.SKILLS_WRITE)
    assert caught.value.code is ErrorCode.UNAUTHORIZED_SCOPE
    assert caught.value.code == "UNAUTHORIZED_SCOPE"
    assert caught.value.scope == "skills:write"
    assert isinstance(caught.value, AuthError)


@pytest.mark.parametrize("scope", [Scope.MEMORY_WRITE, Scope.SKILLS_WRITE])
def test_writes_disabled_blocks_a_granted_write_scope(scope: Scope) -> None:
    """The "writes on" badge has to actually cut writes, grant or no grant (PRD §7.1)."""
    ctx = make_context(scopes=ALL_SCOPES, writes_enabled=False)
    assert ctx.has(scope)  # the grant is real...
    with pytest.raises(ScopeError) as caught:  # ...and still refused
        require_scope(ctx, scope)
    assert caught.value.code is ErrorCode.UNAUTHORIZED_SCOPE
    assert "writes are disabled" in str(caught.value)


@pytest.mark.parametrize("scope", [Scope.MEMORY_READ, Scope.SKILLS_READ, Scope.APIS_USE])
def test_writes_disabled_does_not_block_read_scopes(scope: Scope) -> None:
    require_scope(make_context(scopes=ALL_SCOPES, writes_enabled=False), scope)


@pytest.mark.parametrize("scope", sorted(ALL_SCOPES))
def test_an_empty_grant_refuses_everything(scope: Scope) -> None:
    ctx = make_context(scopes=frozenset(), writes_enabled=True)
    with pytest.raises(ScopeError):
        require_scope(ctx, scope)


@pytest.mark.parametrize("scope", sorted(ALL_SCOPES))
def test_a_full_grant_with_writes_on_allows_everything(scope: Scope) -> None:
    require_scope(make_context(scopes=ALL_SCOPES, writes_enabled=True), scope)


def test_authentication_error_is_one_fixed_opaque_failure() -> None:
    """Every rejection is the same rejection — the DB tests lean on this being constant."""
    first, second = AuthenticationError(), AuthenticationError()
    assert str(first) == str(second) == AUTHENTICATION_FAILED_MESSAGE
    assert first.code is ErrorCode.UNAUTHENTICATED
    assert isinstance(first, AuthError)
    # The constructor takes no detail argument, so no call site can widen it.
    with pytest.raises(TypeError):
        AuthenticationError("no such token")  # type: ignore[call-arg]


def test_scope_error_message_names_the_scope_not_the_credential() -> None:
    ctx = make_context(scopes=frozenset())
    with pytest.raises(ScopeError) as caught:
        require_scope(ctx, Scope.APIS_MANAGE)
    message = str(caught.value)
    assert "apis:manage" in message
    assert str(ctx.connection_id) not in message
