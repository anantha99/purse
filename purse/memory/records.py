"""Plain value objects the memory service hands back (C3.1).

Why not just return the SQLAlchemy models? Because a ``Memory`` is bound to the
session that loaded it: read an attribute after the session closes and you get a
``DetachedInstanceError``, and every consumer (REST now, MCP next, the web UI
after that) would have to know the transaction lifetime of a row it merely wants
to serialise. These are frozen snapshots — safe to return, safe to cache, safe
to hand to a JSON encoder.

They also give the memory API a surface that does not change when the schema
does: ``embedding`` is deliberately absent (derived, droppable, never a client's
business) and ``workspace_id`` is absent from the public shapes because the
caller's connection already determines it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from purse.db.models import InitiatedBy, Memory, MemoryCurrent, MemoryKind

__all__ = ["MemoryRecord", "Provenance", "SearchHit"]


@dataclass(frozen=True, slots=True)
class Provenance:
    """Who wrote a memory, and on whose say-so.

    ``connection_id`` is proven by the gateway. ``agent_id`` and ``initiated_by``
    are claims the calling agent made about itself (PRD §10, C4.7) — recorded
    faithfully, trusted no further than that.
    """

    connection_id: uuid.UUID
    agent_id: str | None
    initiated_by: InitiatedBy

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection_id": str(self.connection_id),
            "agent_id": self.agent_id,
            "initiated_by": self.initiated_by.value,
        }


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A canonical memory row, detached from its session."""

    id: uuid.UUID
    content: str
    kind: MemoryKind
    created_at: dt.datetime
    provenance: Provenance
    supersedes: uuid.UUID | None = None
    tombstone: bool = False

    @classmethod
    def from_model(cls, row: Memory | MemoryCurrent) -> MemoryRecord:
        """Snapshot a loaded ``memories`` / ``memories_current`` row."""
        return cls(
            id=row.id,
            content=row.content,
            kind=row.kind,
            created_at=row.created_at,
            provenance=Provenance(
                connection_id=row.connection_id,
                agent_id=row.agent_id,
                initiated_by=row.initiated_by,
            ),
            supersedes=row.supersedes,
            tombstone=row.tombstone,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "content": self.content,
            "kind": self.kind.value,
            "created_at": self.created_at.isoformat(),
            "supersedes": str(self.supersedes) if self.supersedes is not None else None,
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked result from :func:`purse.memory.service.search_memory`.

    ``score`` is ``None`` for the canonical fallback path, which has no notion of
    relevance beyond "matched, and is recent". A real number arrives with the
    Mem0 engine (C3.4/C3.5).
    """

    id: uuid.UUID
    content: str
    kind: MemoryKind
    created_at: dt.datetime
    provenance: Provenance
    score: float | None = None

    @classmethod
    def from_record(cls, record: MemoryRecord, *, score: float | None = None) -> SearchHit:
        return cls(
            id=record.id,
            content=record.content,
            kind=record.kind,
            created_at=record.created_at,
            provenance=record.provenance,
            score=score,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "content": self.content,
            "kind": self.kind.value,
            "created_at": self.created_at.isoformat(),
            "score": self.score,
            "provenance": self.provenance.as_dict(),
        }
