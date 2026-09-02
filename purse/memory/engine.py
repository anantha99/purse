"""The ``MemoryEngine`` interface (C3.3) and the no-op engine (C3.8).

PRD §8.2 splits memory in two:

* the **canonical store** — an append-only Postgres log, the source of truth,
  what an export contains, what supersession and tombstones act on;
* the **derived index** — Mem0 OSS (C3.4), or pgvector (C3.5), or something else
  later. It holds embeddings, extractions, consolidations. It is *droppable*:
  ``drop()`` then ``rebuild()`` from the canonical log must produce an
  equivalent index (C3.6 is the command that proves it).

This module is the seam between the two. Implementations go behind it; nothing
above it knows which engine is installed.

.. rubric:: The rule that matters

**An engine failure must never fail a canonical write.**

The canonical insert is the product's promise; the index is an accelerator. If
Mem0's LLM extraction call times out, if the embedding provider 500s, if the
vector store is mid-restart — the memory is still saved, still exported, still
returned by ``list_memories``. It is merely not semantically searchable until
the index catches up or is rebuilt.

That rule is enforced at the *call site*, not here: implementations of
:meth:`MemoryEngine.ingest` are free to raise whatever they like, and
:func:`purse.memory.service.add_memory` catches ``Exception`` around every
engine call, logs at ``warning``, and returns the canonical record. Engines
should therefore raise honestly rather than swallowing errors — a swallowed
error is an index that is silently, permanently stale.

``search`` is the one method whose failure is visible to a caller: the service
treats an engine error there as "no engine results" and falls back to the
canonical text search, so a broken index degrades recall rather than the API.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from purse.memory.records import MemoryRecord

__all__ = ["EngineHit", "MemoryEngine", "NullEngine"]


@dataclass(frozen=True, slots=True)
class EngineHit:
    """A pointer into the canonical store, plus a relevance score.

    Deliberately *not* the memory content. The index may hold Mem0's rewritten,
    consolidated phrasing — useful for matching, wrong to show a user as if the
    vault contained it (PRD §8.2: "Mem0's rewrites never touch canonical rows").
    The service hydrates every hit from Postgres and drops ids that are no longer
    current, so a stale index can never resurrect a tombstoned memory.

    ``content`` is carried only as a debugging courtesy — what the index matched
    on. Consumers must not display it.
    """

    memory_id: uuid.UUID
    score: float | None = None
    content: str | None = None


class MemoryEngine(ABC):
    """The derived-index contract (PRD §8.2). Swappable by design.

    Implementations are constructed with whatever configuration they need and
    passed into the service functions; the service never constructs one.
    """

    @abstractmethod
    def ingest(self, record: MemoryRecord, *, workspace_id: uuid.UUID) -> None:
        """Index one canonical record.

        Called after the canonical insert has already succeeded. May be slow,
        may be asynchronous internally (C3.4 puts Mem0 behind a background
        queue), may fail — see the module docstring: the caller catches.
        """

    @abstractmethod
    def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[EngineHit]:
        """Ranked candidates for *query*, best first, at most *limit*.

        Returning ``[]`` means "no opinion" and hands the query to the canonical
        fallback. Implementations must scope results to *workspace_id*; the
        service re-checks against Postgres, but an index that mixes workspaces
        is already a bug.
        """

    @abstractmethod
    def rebuild(self, workspace_id: uuid.UUID, records: Iterable[MemoryRecord]) -> None:
        """Replace this workspace's index from the canonical log (C3.6).

        *records* is the current-view stream. Implementations should treat this
        as ``drop`` + full ingest, and must be safe to run repeatedly.
        """

    @abstractmethod
    def drop(self, workspace_id: uuid.UUID) -> None:
        """Discard everything indexed for a workspace. Never touches canonical rows."""


class NullEngine(MemoryEngine):
    """An engine that indexes nothing and finds nothing.

    The M1 default, and the right choice whenever the index is unavailable: with
    it installed, writes still land canonically, and ``search_memory`` falls back
    to the Postgres ``ILIKE`` path. Semantic recall arrives when a real engine is
    wired in (C3.4/C3.5) — no call site changes.

    Also the correct engine for tests that are about the canonical path.
    """

    def ingest(self, record: MemoryRecord, *, workspace_id: uuid.UUID) -> None:
        return None

    def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[EngineHit]:
        return []

    def rebuild(self, workspace_id: uuid.UUID, records: Iterable[MemoryRecord]) -> None:
        return None

    def drop(self, workspace_id: uuid.UUID) -> None:
        return None
