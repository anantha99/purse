"""The authenticated caller, as the memory service needs to see it (C3.1).

The memory service records provenance on every write: which connection did it,
which agent claimed to be driving. That information comes from the auth layer
(C2), but memory must not *import* the auth layer — the dependency runs the
other way (gateway → auth, gateway → memory), and a concrete import would make
the memory tests need a real connection-authentication stack to construct a
caller.

So the contract is structural. :class:`WriteContext` is a plain
:class:`~typing.Protocol`: anything with the three attributes satisfies it, with
no inheritance, no registration, and no import in either direction. Tests pass a
frozen dataclass.

Note which object satisfies it in production: **not** ``purse.auth``'s
``AuthContext``, which has ``connection_id`` and ``workspace_id`` but no
``agent_id`` — correctly, because a token identifies a connection, not an agent
(PRD §10 makes ``agent_id`` a self-reported per-call claim). The gateway
composes the two into a ``purse.gateway.rest.RequestContext``, and that is what
arrives here.

Not ``@runtime_checkable`` on purpose: ``isinstance`` against a runtime-checkable
Protocol only checks that the attribute *names* exist, which is a weaker
guarantee than the one mypy already gives at every call site, and reads as a
security check while being none.
"""

from __future__ import annotations

import uuid
from typing import Protocol

__all__ = ["WriteContext"]


class WriteContext(Protocol):
    """The authenticated caller behind a memory operation.

    ``connection_id`` is the *trusted* provenance (PRD §10, C4.7): it is what
    the gateway proved by validating a token. ``agent_id`` is a self-reported
    claim and is stored as one.
    """

    @property
    def connection_id(self) -> uuid.UUID:
        """The authenticated connection. Recorded on every memory row and audit entry."""
        ...

    @property
    def workspace_id(self) -> uuid.UUID:
        """The workspace this connection is bound to. The isolation boundary."""
        ...

    @property
    def agent_id(self) -> str | None:
        """Optional self-reported agent identifier. Untrusted; provenance only."""
        ...
