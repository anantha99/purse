"""The authenticated caller, and the gate every tool call passes through (C2.2).

An :class:`AuthContext` is what a verified credential turns into. It is the
only thing the MCP and REST layers should carry around: it names the workspace
(so nothing downstream has to be trusted to pick the right one), the granted
scopes, and whether writes are on.

Frozen and slotted, because a context that can be mutated after the check is a
context whose check means nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from purse.auth.errors import ScopeError
from purse.auth.scopes import Scope, format_scopes, has_scope

__all__ = ["AuthContext", "require_scope"]


@dataclass(frozen=True, slots=True)
class AuthContext:
    """A verified caller: who they are, where they may act, and what they may do.

    ``connection_id`` is the trusted provenance recorded on every write (PRD
    §10 / C4.7) — unlike ``initiated_by``, which is a claim the agent makes.
    """

    connection_id: uuid.UUID
    workspace_id: uuid.UUID
    scopes: frozenset[Scope]
    writes_enabled: bool
    client_name: str

    def has(self, scope: Scope) -> bool:
        """True when this connection was granted *scope*. Ignores the writes toggle."""
        return has_scope(self.scopes, scope)

    def scope_strings(self) -> list[str]:
        """Granted scopes as sorted strings, for display and audit."""
        return format_scopes(self.scopes)


def require_scope(ctx: AuthContext, scope: Scope) -> None:
    """Authorise *scope* on *ctx*, or raise :class:`ScopeError`.

    Two independent conditions, both of which must hold:

    1. the scope was granted to this connection;
    2. if it is a ``:write`` scope, the connection has writes enabled.

    The second check is the belt to the first's braces. A connection's granted
    scopes and its ``writes_enabled`` flag are separate columns edited by
    separate UI surfaces — the scope list on the connection screen, the "writes
    on" badge with its one-tap toggle (PRD §7.1) — and the toggle has to
    actually cut writes even when the grant still says ``memory:write``.
    Otherwise flipping the badge off is theatre.
    """
    if not ctx.has(scope):
        raise ScopeError(
            f"this connection is not granted {scope.value}",
            scope=scope.value,
        )
    if scope.is_write and not ctx.writes_enabled:
        raise ScopeError(
            f"writes are disabled for this connection, so {scope.value} cannot be used",
            scope=scope.value,
        )
