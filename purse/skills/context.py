"""The authenticated caller, as the skills service needs to see it (C5.2).

The skills service records provenance on every write (which connection upserted
a version) and is scoped to exactly one workspace on every call. Both facts come
from the auth layer (C2), but — exactly as with :mod:`purse.memory.context` —
skills must not *import* the auth layer: the dependency runs gateway → auth and
gateway → skills, never the other way, so a concrete import would make the skills
tests need a real authentication stack just to construct a caller.

So the contract is structural. :class:`SkillContext` is a plain
:class:`~typing.Protocol`: anything with the two attributes satisfies it, with no
inheritance and no import in either direction. The same objects that satisfy
:class:`purse.memory.context.WriteContext` satisfy this too — ``purse.auth``'s
``AuthContext``, the gateway's ``RequestContext`` / ``_ToolContext``, and the
tests' ``StubContext`` — because every one of them carries ``connection_id`` and
``workspace_id``.

Skills need no ``agent_id``: unlike a memory, a skill version is not attributed
to a self-reported agent, only to the connection that wrote it (the trusted
provenance) and audited as such.
"""

from __future__ import annotations

import uuid
from typing import Protocol

__all__ = ["SkillContext"]


class SkillContext(Protocol):
    """The authenticated caller behind a skills operation."""

    @property
    def connection_id(self) -> uuid.UUID:
        """The authenticated connection. Recorded on every ``skill.upsert`` audit entry."""
        ...

    @property
    def workspace_id(self) -> uuid.UUID:
        """The workspace this connection is bound to. The isolation boundary."""
        ...
