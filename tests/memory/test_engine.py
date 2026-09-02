"""The ``MemoryEngine`` contract (C3.3) — no database.

Two things are worth proving without Postgres:

1. the interface is a real interface (an incomplete engine cannot be
   instantiated), so C3.4's Mem0 adapter cannot half-implement it; and
2. :class:`NullEngine` is inert in exactly the way the M1 default needs to be.

The rule that an engine failure must never fail a canonical write is proved
against a real database in ``test_service_db.py`` — it is a property of the
service, not of the engine.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from purse.db.models import InitiatedBy, MemoryKind
from purse.memory.engine import EngineHit, MemoryEngine, NullEngine
from purse.memory.records import MemoryRecord, Provenance


def _record() -> MemoryRecord:
    return MemoryRecord(
        id=uuid.uuid4(),
        content="I prefer TypeScript.",
        kind=MemoryKind.PREFERENCE,
        created_at=dt.datetime.now(dt.UTC),
        provenance=Provenance(
            connection_id=uuid.uuid4(), agent_id=None, initiated_by=InitiatedBy.USER
        ),
    )


def test_the_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        MemoryEngine()  # type: ignore[abstract]


def test_a_partial_implementation_cannot_be_instantiated() -> None:
    """C3.4 must not be able to ship a Mem0 adapter that forgets ``drop``.

    ``drop`` is the one C3.6 (index rebuild) depends on, and a silently missing
    one would look like a working engine right up until someone tried to prove
    the index is droppable.
    """

    class HalfAnEngine(MemoryEngine):
        def ingest(self, record: MemoryRecord, *, workspace_id: uuid.UUID) -> None:
            return None

        def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[EngineHit]:
            return []

    with pytest.raises(TypeError) as caught:
        HalfAnEngine()  # type: ignore[abstract]
    assert "drop" in str(caught.value)
    assert "rebuild" in str(caught.value)


def test_null_engine_is_inert() -> None:
    engine = NullEngine()
    workspace_id = uuid.uuid4()

    engine.ingest(_record(), workspace_id=workspace_id)
    engine.rebuild(workspace_id, [_record()])
    engine.drop(workspace_id)
    assert engine.search(workspace_id, "typescript", 8) == []


def test_null_engine_finds_nothing_even_for_what_it_just_ingested() -> None:
    """Not a bug — the reason ``search_memory`` needs a canonical fallback at all."""
    engine = NullEngine()
    workspace_id = uuid.uuid4()
    record = _record()
    engine.ingest(record, workspace_id=workspace_id)
    assert engine.search(workspace_id, record.content, 8) == []


def test_engine_hit_carries_a_pointer_not_the_content() -> None:
    """PRD §8.2: Mem0's rewrites must never be shown as if they were canonical.

    The hit's required field is an id; ``content`` is optional and documented as
    debugging-only, which is what forces the service to hydrate from Postgres.
    """
    memory_id = uuid.uuid4()
    hit = EngineHit(memory_id=memory_id)
    assert hit.memory_id == memory_id
    assert hit.score is None
    assert hit.content is None
