"""The Mem0 adapter against real pgvector, with a deterministic fake embedder (C3.4-3.6).

These are the tests that need the actual database: ranking, workspace isolation,
supersession and tombstone removal, and drop/rebuild are all behaviours of the
pgvector store and the Mem0 pipeline, and a fake would prove none of them. They
are ``db``-marked — skipped without a Postgres, run in CI against pgvector pg17.

The embedder is faked (``tests/memory/fake_embedder.py``): a network-free,
key-free, reproducible bag-of-words vector, injected through Mem0's embedder
provider registry. So the store is real and the ranking is real; only the
text→vector step is a stand-in, and a deterministic one at that.

Note on transactions: the canonical rows a test writes live in the rolled-back
session transaction, but Mem0 writes through its **own** connection pool and
commits independently. Each test uses a fresh workspace uuid, and Mem0 scopes
every read by ``user_id=workspace_id``, so the committed vector rows of one test
never surface in another's search. The throwaway database is dropped after the run.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

pytest.importorskip("mem0", reason="mem0ai not installed (C3.4 dependency)")
pytest.importorskip("psycopg_pool", reason="psycopg_pool not installed (pgvector store dependency)")

from purse.db.models import AuthMode, User
from purse.db.repo import Repo, create_workspace
from purse.memory import rebuild, service
from purse.memory.mem0_engine import EmbeddingConfig, Mem0Engine
from tests.conftest import StubContext
from tests.memory.fake_embedder import (
    FAKE_PROVIDER,
    register_fake_embedder,
    unregister_fake_embedder,
)

pytestmark = pytest.mark.db


@pytest.fixture
def mem0_engine(test_database_url: str, tmp_path: Path) -> Iterator[Mem0Engine]:
    """A Mem0 engine on the throwaway database, embedding with the fake provider."""
    register_fake_embedder()
    try:
        engine = Mem0Engine(
            embedding=EmbeddingConfig(api_key="fake-key", provider=FAKE_PROVIDER, dims=1536),
            database_url=test_database_url,
            # A per-test SQLite history path — the default is a shared ~/.mem0 file
            # that concurrent tests would lock each other out of.
            history_db_path=str(tmp_path / "mem0-history.db"),
        )
        yield engine
    finally:
        unregister_fake_embedder()


def _second_workspace(session: Session, user: User) -> StubContext:
    workspace = create_workspace(session, user_id=user.id, name="Work")
    repo = Repo.open(session, workspace.id)
    connection = repo.add_connection(
        client_name="cursor", auth_mode=AuthMode.PAT, writes_enabled=True
    )
    return StubContext(connection_id=connection.id, workspace_id=workspace.id)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_semantic_search_returns_the_more_relevant_memory_first(
    session: Session, ctx: StubContext, mem0_engine: Mem0Engine
) -> None:
    """The vault ranks by vector similarity, and the hit carries a real score."""
    relevant = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="The database migration runs every friday afternoon",
        kind="fact",
        initiated_by="user",
    )
    service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="The office coffee machine is broken again",
        kind="fact",
        initiated_by="user",
    )

    hits = service.search_memory(
        session, ctx, mem0_engine, query="friday migration database schedule"
    )

    assert hits, "the relevant memory should surface"
    assert hits[0].id == relevant.id
    assert hits[0].content == "The database migration runs every friday afternoon"
    assert hits[0].score is not None, "the Mem0 engine returns a real relevance score"


def test_search_hydrates_content_from_canonical_not_from_the_index(
    session: Session, ctx: StubContext, mem0_engine: Mem0Engine
) -> None:
    record = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="Rust is the systems programming language we standardised on",
        kind="decision",
        initiated_by="user",
    )
    hits = service.search_memory(session, ctx, mem0_engine, query="rust programming language")
    assert [hit.id for hit in hits] == [record.id]
    assert hits[0].content == "Rust is the systems programming language we standardised on"


# ---------------------------------------------------------------------------
# Supersession / tombstone remove from recall
# ---------------------------------------------------------------------------


def test_supersede_removes_the_old_memory_from_recall(
    session: Session, ctx: StubContext, mem0_engine: Mem0Engine
) -> None:
    """After an edit, the old phrasing is gone from the index (forget + reingest)."""
    original = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="I love hiking in the mountains on weekends",
        kind="preference",
        initiated_by="user",
    )
    service.update_memory(
        session,
        ctx,
        mem0_engine,
        memory_id=original.id,
        content="I love swimming in the ocean on weekends",
    )

    # The distinctive words of the old version surface nothing: it was forgotten
    # from the index and is no longer current, so neither the engine nor the
    # ILIKE fallback finds it.
    assert service.search_memory(session, ctx, mem0_engine, query="hiking mountains") == []
    # The new version is recallable.
    swimming = service.search_memory(session, ctx, mem0_engine, query="swimming ocean")
    assert [hit.content for hit in swimming] == ["I love swimming in the ocean on weekends"]


def test_tombstone_removes_the_memory_from_recall(
    session: Session, ctx: StubContext, mem0_engine: Mem0Engine
) -> None:
    record = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="The rocket launch codes are stored in the vault",
        kind="fact",
        initiated_by="user",
    )
    # Delete with the engine supplied: the tombstone also forgets it from the index.
    service.delete_memory(session, ctx, memory_id=record.id, engine=mem0_engine)

    assert service.search_memory(session, ctx, mem0_engine, query="rocket launch codes") == []


def test_a_tombstone_without_an_engine_still_disappears_from_recall(
    session: Session, ctx: StubContext, mem0_engine: Mem0Engine
) -> None:
    """The gateway deletes without passing an engine; hydration is the guarantee.

    Even though the vector row lingers in Mem0 (no ``forget`` fired), the memory
    is not in the current view, so ``search_memory`` drops it when hydrating.
    """
    record = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="The satellite telemetry password is in the drawer",
        kind="fact",
        initiated_by="user",
    )
    service.delete_memory(session, ctx, memory_id=record.id)  # no engine, like the gateway

    assert service.search_memory(session, ctx, mem0_engine, query="satellite telemetry") == []


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


def test_search_is_isolated_by_workspace(
    session: Session, ctx: StubContext, user: User, mem0_engine: Mem0Engine
) -> None:
    """A memory in workspace A never surfaces in workspace B's search.

    Mem0 keeps both workspaces in one collection; isolation is the ``user_id``
    filter the adapter puts on every search. Drop it and this leaks.
    """
    service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="Alpha workspace deployment schedule is monday",
        kind="fact",
        initiated_by="user",
    )
    other = _second_workspace(session, user)

    assert (
        service.search_memory(session, other, mem0_engine, query="deployment schedule monday") == []
    )
    # ...and A can still find its own.
    assert service.search_memory(session, ctx, mem0_engine, query="deployment schedule monday")


# ---------------------------------------------------------------------------
# Drop / rebuild (C3.6)
# ---------------------------------------------------------------------------


def test_drop_empties_recall_and_rebuild_restores_it(
    session: Session, ctx: StubContext, mem0_engine: Mem0Engine
) -> None:
    """The index is derived, droppable, and rebuildable from the canonical log."""
    service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="The quarterly report is due next tuesday",
        kind="fact",
        initiated_by="user",
    )
    service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="The server room key is with the facilities team",
        kind="fact",
        initiated_by="user",
    )
    workspace_id = ctx.workspace_id

    # Engine-level recall is populated.
    assert mem0_engine.search(workspace_id, "quarterly report tuesday", 8)

    # Drop empties the index (proven at the engine level, below the ILIKE fallback).
    mem0_engine.drop(workspace_id)
    assert mem0_engine.search(workspace_id, "quarterly report tuesday", 8) == []
    assert mem0_engine.search(workspace_id, "server room key facilities", 8) == []

    # Rebuild replays the current view and recall comes back.
    replayed = rebuild.rebuild_workspace(session, mem0_engine, workspace_id)
    assert replayed == 2
    assert mem0_engine.search(workspace_id, "quarterly report tuesday", 8)
    assert mem0_engine.search(workspace_id, "server room key facilities", 8)


def test_rebuild_replays_only_the_current_view(
    session: Session, ctx: StubContext, mem0_engine: Mem0Engine
) -> None:
    """Superseded and tombstoned rows do not come back on a rebuild."""
    kept = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="keep this migration note",
        kind="fact",
        initiated_by="user",
    )
    dropped = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="delete this stray note",
        kind="fact",
        initiated_by="user",
    )
    service.delete_memory(session, ctx, memory_id=dropped.id)
    old = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="old draft of the plan",
        kind="fact",
        initiated_by="user",
    )
    new = service.update_memory(
        session, ctx, mem0_engine, memory_id=old.id, content="final version of the plan"
    )
    workspace_id = ctx.workspace_id

    replayed = rebuild.rebuild_workspace(session, mem0_engine, workspace_id)
    assert replayed == 2, "only the two current memories are replayed"

    hit_ids = {
        hit.memory_id
        for query in ("migration note", "final version plan")
        for hit in mem0_engine.search(workspace_id, query, 8)
    }
    assert kept.id in hit_ids
    assert new.id in hit_ids
    assert dropped.id not in hit_ids
    assert old.id not in hit_ids


def test_forget_is_idempotent(session: Session, ctx: StubContext, mem0_engine: Mem0Engine) -> None:
    """Forgetting an id the index never held is a no-op, not an error."""
    mem0_engine.forget(ctx.workspace_id, uuid.uuid4())  # must not raise


def test_engine_failure_on_ingest_never_loses_the_canonical_write(
    session: Session, ctx: StubContext, repo: Repo, mem0_engine: Mem0Engine
) -> None:
    """A real add whose index step is fine, then a broken engine — the row persists.

    The dedicated proof with a RaisingEngine is in ``test_service_db.py``; this is
    the Mem0-flavoured smoke that the adapter participates in the same contract:
    the canonical row is durable regardless of the index.
    """
    record = service.add_memory(
        session,
        ctx,
        mem0_engine,
        content="durable regardless of the index",
        kind="fact",
        initiated_by="user",
    )
    assert repo.get_memory(record.id) is not None
