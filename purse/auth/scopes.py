"""Per-connection scopes (C2.2, PRD §7.1/§8.5).

Six scopes, three resources, and nothing else. The set is closed on purpose:
an unrecognised scope string is a bug or an attack, never a feature flag, so
:func:`parse_scope` rejects it loudly rather than ignoring it. Silently
dropping an unknown scope is how a typo becomes an unnoticed privilege
difference between what the UI shows and what the gateway enforces.

Two grant sets are named here because the PRD names them (§7.1):

* :data:`ONBOARDING_SCOPES` — the first connection, provisioned with writes on
  and the "writes on" badge showing.
* :data:`DEFAULT_CONNECTION_SCOPES` — every later connection, read-only until
  the user opts in.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

__all__ = [
    "ALL_SCOPES",
    "DEFAULT_CONNECTION_SCOPES",
    "ONBOARDING_SCOPES",
    "WRITE_SCOPES",
    "Scope",
    "UnknownScopeError",
    "format_scopes",
    "has_scope",
    "parse_scope",
    "parse_scopes",
]


class UnknownScopeError(ValueError):
    """Raised when a scope string is not one of the six defined scopes."""


class Scope(enum.StrEnum):
    """The complete scope vocabulary. ``str(Scope.MEMORY_READ) == 'memory:read'``."""

    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    SKILLS_READ = "skills:read"
    SKILLS_WRITE = "skills:write"
    APIS_USE = "apis:use"
    APIS_MANAGE = "apis:manage"

    @property
    def is_write(self) -> bool:
        """True for the ``:write`` scopes the "writes on" badge governs.

        ``apis:manage`` is deliberately **not** in this set. It is an
        administrative scope over stored credentials (C6) rather than a write
        into the vault's memory/skills content, it is never granted to an MCP
        connection in M1, and the badge the PRD describes is about agent
        writes. Revisit when C6.2 hands ``apis:manage`` to a connection.
        """
        return self.value.endswith(":write")


#: Every scope, in declaration order.
ALL_SCOPES: frozenset[Scope] = frozenset(Scope)

#: The scopes a ``writes_enabled=False`` connection can never exercise.
WRITE_SCOPES: frozenset[Scope] = frozenset(scope for scope in Scope if scope.is_write)

#: The onboarding connection: ``memory:*`` plus ``skills:read`` (PRD §7.1).
ONBOARDING_SCOPES: frozenset[Scope] = frozenset(
    {Scope.MEMORY_READ, Scope.MEMORY_WRITE, Scope.SKILLS_READ}
)

#: Every connection after the first: read-only until the user opts in (PRD §7.1).
DEFAULT_CONNECTION_SCOPES: frozenset[Scope] = frozenset({Scope.MEMORY_READ, Scope.SKILLS_READ})


def parse_scope(value: str) -> Scope:
    """Convert one scope string to a :class:`Scope`.

    Raises :class:`UnknownScopeError` for anything else — including case
    variants and whitespace-padded values. Scope strings are machine-generated
    and stored verbatim; normalising them here would only paper over the bug
    that produced the odd spelling.
    """
    try:
        return Scope(value)
    except ValueError:
        known = ", ".join(sorted(scope.value for scope in Scope))
        raise UnknownScopeError(f"unknown scope {value!r}; known scopes are: {known}") from None


def parse_scopes(values: Iterable[str]) -> frozenset[Scope]:
    """Convert an iterable of scope strings to a set, rejecting any unknown one.

    All-or-nothing: one bad entry fails the whole grant, because a partially
    applied grant is a privilege level nobody chose.
    """
    return frozenset(parse_scope(value) for value in values)


def format_scopes(scopes: Iterable[Scope]) -> list[str]:
    """Scope strings in a stable (alphabetical) order, for storage and display.

    Sets have no order; a stored ``text[]`` column does, and a column whose
    order wobbles between writes makes diffs and audit trails unreadable.
    """
    return sorted(scope.value for scope in scopes)


def has_scope(granted: Iterable[Scope], required: Scope) -> bool:
    """True when *required* is in *granted*.

    Flat membership, no wildcard or hierarchy: ``memory:write`` does not imply
    ``memory:read``. Implication rules are the kind of thing that is obvious to
    whoever wrote them and surprising to everyone else, so grants list every
    scope they mean.
    """
    return required in frozenset(granted)
