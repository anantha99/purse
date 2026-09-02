"""C2.2: the scope vocabulary is closed, and the named grant sets are the PRD's."""

from __future__ import annotations

import pytest

from purse.auth.scopes import (
    ALL_SCOPES,
    DEFAULT_CONNECTION_SCOPES,
    ONBOARDING_SCOPES,
    WRITE_SCOPES,
    Scope,
    UnknownScopeError,
    format_scopes,
    has_scope,
    parse_scope,
    parse_scopes,
)

EXPECTED = {
    "memory:read",
    "memory:write",
    "skills:read",
    "skills:write",
    "apis:use",
    "apis:manage",
}


def test_the_vocabulary_is_exactly_the_six_prd_scopes() -> None:
    every_member = frozenset(Scope)
    assert {scope.value for scope in Scope} == EXPECTED
    assert every_member == ALL_SCOPES


def test_scope_stringifies_to_its_wire_value() -> None:
    assert str(Scope.MEMORY_WRITE) == "memory:write"
    assert f"{Scope.APIS_USE}" == "apis:use"


@pytest.mark.parametrize("value", sorted(EXPECTED))
def test_every_known_scope_round_trips(value: str) -> None:
    assert parse_scope(value).value == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "memory",
        "memory:*",
        "memory:admin",
        "Memory:Read",
        "memory:read ",
        " memory:read",
        "MEMORY:READ",
        "secrets:read",
        "admin",
        "*",
    ],
)
def test_unknown_scopes_are_rejected_loudly(value: str) -> None:
    with pytest.raises(UnknownScopeError):
        parse_scope(value)


def test_unknown_scope_error_lists_what_is_valid() -> None:
    with pytest.raises(UnknownScopeError) as caught:
        parse_scope("memory:admin")
    message = str(caught.value)
    assert "memory:admin" in message
    assert "memory:read" in message


def test_parse_scopes_is_all_or_nothing() -> None:
    """One bad entry fails the grant; a partially applied grant is nobody's decision."""
    with pytest.raises(UnknownScopeError):
        parse_scopes(["memory:read", "memory:admin"])


def test_parse_scopes_builds_a_set() -> None:
    assert parse_scopes(["memory:read", "skills:read", "memory:read"]) == frozenset(
        {Scope.MEMORY_READ, Scope.SKILLS_READ}
    )
    assert parse_scopes([]) == frozenset()


def test_format_scopes_is_stable_and_sorted() -> None:
    scopes = [Scope.SKILLS_READ, Scope.MEMORY_WRITE, Scope.APIS_USE]
    assert format_scopes(scopes) == ["apis:use", "memory:write", "skills:read"]
    assert format_scopes(reversed(scopes)) == format_scopes(scopes)


# -- has_scope matrix --------------------------------------------------------


@pytest.mark.parametrize(
    ("granted", "required", "expected"),
    [
        (frozenset(), Scope.MEMORY_READ, False),
        (frozenset({Scope.MEMORY_READ}), Scope.MEMORY_READ, True),
        (frozenset({Scope.MEMORY_READ}), Scope.MEMORY_WRITE, False),
        # No implication in either direction: write does not grant read...
        (frozenset({Scope.MEMORY_WRITE}), Scope.MEMORY_READ, False),
        # ...and one resource never grants another.
        (frozenset({Scope.MEMORY_READ}), Scope.SKILLS_READ, False),
        (frozenset({Scope.APIS_MANAGE}), Scope.APIS_USE, False),
        (ALL_SCOPES, Scope.APIS_MANAGE, True),
        (ONBOARDING_SCOPES, Scope.MEMORY_WRITE, True),
        (ONBOARDING_SCOPES, Scope.SKILLS_WRITE, False),
        (DEFAULT_CONNECTION_SCOPES, Scope.MEMORY_READ, True),
        (DEFAULT_CONNECTION_SCOPES, Scope.MEMORY_WRITE, False),
    ],
)
def test_has_scope_matrix(granted: frozenset[Scope], required: Scope, expected: bool) -> None:
    assert has_scope(granted, required) is expected


def test_has_scope_accepts_any_iterable() -> None:
    assert has_scope([Scope.MEMORY_READ], Scope.MEMORY_READ)
    assert has_scope((s for s in [Scope.MEMORY_READ]), Scope.MEMORY_READ)


# -- named grant sets --------------------------------------------------------


def test_onboarding_grant_is_memory_star_plus_skills_read() -> None:
    """PRD §7.1: the first connection can write memories, read skills, nothing else."""
    expected = frozenset({Scope.MEMORY_READ, Scope.MEMORY_WRITE, Scope.SKILLS_READ})
    assert expected == ONBOARDING_SCOPES


def test_later_connections_default_to_read_only() -> None:
    expected = frozenset({Scope.MEMORY_READ, Scope.SKILLS_READ})
    assert expected == DEFAULT_CONNECTION_SCOPES
    assert not DEFAULT_CONNECTION_SCOPES & WRITE_SCOPES


def test_write_scopes_are_the_write_suffixed_ones() -> None:
    expected = frozenset({Scope.MEMORY_WRITE, Scope.SKILLS_WRITE})
    assert expected == WRITE_SCOPES
    assert all(scope.is_write for scope in WRITE_SCOPES)
    assert not any(scope.is_write for scope in ALL_SCOPES - WRITE_SCOPES)
