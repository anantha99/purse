"""An engine failure never fails a canonical write (C3.3) — the unit half.

The whole-path proof (write survives, row is queryable afterwards) needs a
database and lives in ``test_service_db.py``. What is testable without one is
the seam itself: the two places the service touches an engine both swallow and
log, and nothing else in the service touches an engine at all.

That last assertion is the one with teeth. It is easy to add a fourth engine
call during C3.4 and forget the ``try`` — so this walks the module's source and
fails if an engine method is called anywhere but the two guarded helpers.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import logging
import uuid

import pytest

from purse.db.models import InitiatedBy, MemoryKind
from purse.memory import service
from purse.memory.records import MemoryRecord, Provenance
from tests.conftest import EngineFailure, RaisingEngine, RecordingEngine

WORKSPACE_ID = uuid.uuid4()


@pytest.fixture
def record() -> MemoryRecord:
    return MemoryRecord(
        id=uuid.uuid4(),
        content="Deploys go out on Thursdays.",
        kind=MemoryKind.DECISION,
        created_at=dt.datetime.now(dt.UTC),
        provenance=Provenance(
            connection_id=uuid.uuid4(), agent_id="pytest", initiated_by=InitiatedBy.AGENT
        ),
    )


def test_the_raising_engine_really_does_raise(record: MemoryRecord) -> None:
    """Guard against the test below passing because the double is broken."""
    with pytest.raises(EngineFailure):
        RaisingEngine().ingest(record, workspace_id=WORKSPACE_ID)
    with pytest.raises(EngineFailure):
        RaisingEngine().search(WORKSPACE_ID, "thursday", 8)


def test_ingest_failure_is_swallowed(record: MemoryRecord) -> None:
    service._ingest(RaisingEngine(), record, WORKSPACE_ID)


def test_ingest_failure_is_logged_at_warning_with_a_traceback(
    record: MemoryRecord, caplog: pytest.LogCaptureFixture
) -> None:
    """Swallowed is not the same as hidden: a stale index must be visible to an
    operator, because the fix (C3.6 rebuild) is manual."""
    with caplog.at_level(logging.WARNING, logger="purse.memory.service"):
        service._ingest(RaisingEngine(), record, WORKSPACE_ID)

    assert len(caplog.records) == 1
    entry = caplog.records[0]
    assert entry.levelno == logging.WARNING
    assert str(record.id) in entry.getMessage()
    assert entry.exc_info is not None, "the traceback is the whole point of the log line"


def test_search_failure_degrades_to_no_engine_results() -> None:
    assert service._engine_search(RaisingEngine(), WORKSPACE_ID, "thursday", 8) == []


def test_search_failure_is_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="purse.memory.service"):
        service._engine_search(RaisingEngine(), WORKSPACE_ID, "thursday", 8)
    assert len(caplog.records) == 1
    assert "falling back" in caplog.records[0].getMessage()


def test_a_healthy_engine_is_passed_through_untouched(record: MemoryRecord) -> None:
    engine = RecordingEngine()
    service._ingest(engine, record, WORKSPACE_ID)
    assert engine.ingested == [record]

    service._engine_search(engine, WORKSPACE_ID, "thursday", 3)
    assert engine.searched == [(WORKSPACE_ID, "thursday", 3)]


ENGINE_METHODS = frozenset({"ingest", "search", "forget", "rebuild", "drop"})
GUARDED_HELPERS = frozenset({"_ingest", "_engine_search", "_forget"})


def test_no_unguarded_engine_call_exists_in_the_service() -> None:
    """Every ``engine.<method>(...)`` in the service must sit inside a helper
    that catches. Add an engine call somewhere else and this fails, which is the
    point: C3.4 will be tempted to.
    """
    tree = ast.parse(inspect.getsource(service))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name in GUARDED_HELPERS:
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in ENGINE_METHODS
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "engine"
            ):
                offenders.append(f"{node.name} calls engine.{call.func.attr}()")

    assert not offenders, (
        "engine calls outside the guarded helpers: "
        + ", ".join(offenders)
        + ". Route them through service._ingest / service._engine_search, "
        "or an engine outage becomes a failed write."
    )
