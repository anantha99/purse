"""The canonical memory path against a real Postgres (C3.1, C3.2, C3.8).

Everything here is ``db``-marked and skips without a database — see
``tests/conftest.py``. It has to be a real Postgres: the append-only triggers,
the ``memories_current`` view, ``ILIKE``, and row-tuple keyset comparison are all
behaviours of the database, and a fake would prove none of them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from purse.db.models import AuthMode, InitiatedBy, MemoryKind, User
from purse.db.repo import Repo, create_workspace
from purse.memory import errors, service
from purse.memory.engine import EngineHit, NullEngine
from tests.conftest import RaisingEngine, RecordingEngine, StubContext

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_memory_writes_a_canonical_row_with_provenance(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    record = service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="I prefer TypeScript.",
        kind="preference",
        initiated_by="user",
    )

    assert record.content == "I prefer TypeScript."
    assert record.kind is MemoryKind.PREFERENCE
    assert record.supersedes is None
    assert record.tombstone is False
    assert record.created_at is not None

    # The trusted half of provenance is the connection; the rest is a claim.
    assert record.provenance.connection_id == ctx.connection_id
    assert record.provenance.agent_id == "claude-code/1.0"
    assert record.provenance.initiated_by is InitiatedBy.USER

    stored = repo.get_memory(record.id)
    assert stored is not None
    assert stored.content == "I prefer TypeScript."
    assert stored.workspace_id == ctx.workspace_id


def test_add_memory_writes_an_audit_row(session: Session, ctx: StubContext, repo: Repo) -> None:
    record = service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="Deploys on Thursdays.",
        kind="decision",
        initiated_by="agent",
    )

    entries = repo.list_audit()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == "memory.add"
    assert entry.target_type == "memory"
    assert entry.target_id == str(record.id)
    assert entry.connection_id == ctx.connection_id
    assert entry.agent_id == "claude-code/1.0"
    # PRD §13: names and IDs only. The content must never reach the audit log.
    assert "Thursdays" not in entry.target_id


def test_add_memory_reaches_the_engine(session: Session, ctx: StubContext) -> None:
    engine = RecordingEngine()
    record = service.add_memory(
        session, ctx, engine, content="Ship on Fridays.", kind="fact", initiated_by="user"
    )
    assert [ingested.id for ingested in engine.ingested] == [record.id]


def test_an_exploding_engine_does_not_lose_the_canonical_write(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    """The C3.3 promise, proved end to end: Mem0 down ≠ memory lost."""
    record = service.add_memory(
        session,
        ctx,
        RaisingEngine(),
        content="The index is on fire.",
        kind="fact",
        initiated_by="user",
    )

    stored = repo.get_memory(record.id)
    assert stored is not None
    assert stored.content == "The index is on fire."
    # And it is a *current* memory, not merely a row: still listable, still
    # findable by the fallback search.
    page = service.list_memories(session, ctx)
    assert record.id in {item.id for item in page.items}
    hits = service.search_memory(session, ctx, RaisingEngine(), query="on fire")
    assert [hit.id for hit in hits] == [record.id]


def test_add_memory_rejects_oversized_content_before_writing_anything(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    with pytest.raises(errors.PayloadTooLargeError):
        service.add_memory(
            session,
            ctx,
            NullEngine(),
            content="a" * (service.MAX_CONTENT_BYTES + 1),
            kind="fact",
            initiated_by="user",
        )
    assert repo.count_memories() == 0
    assert repo.list_audit() == []


def test_add_memory_rejects_an_unknown_kind_before_writing_anything(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    with pytest.raises(errors.ValidationError):
        service.add_memory(
            session, ctx, NullEngine(), content="hi", kind="profile", initiated_by="user"
        )
    assert repo.count_memories() == 0


# ---------------------------------------------------------------------------
# update / supersession
# ---------------------------------------------------------------------------


def test_update_memory_supersedes_rather_than_mutating(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    original = service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="I prefer Python.",
        kind="preference",
        initiated_by="user",
    )
    updated = service.update_memory(
        session, ctx, NullEngine(), memory_id=original.id, content="I prefer TypeScript."
    )

    assert updated.id != original.id
    assert updated.supersedes == original.id

    # The old row is untouched — it is the history (PRD §8.2).
    old = repo.get_memory(original.id)
    assert old is not None
    assert old.content == "I prefer Python."
    assert old.tombstone is False

    # ...but it is no longer current, and the new one is.
    current_ids = {row.id for row in repo.current_memories()}
    assert current_ids == {updated.id}


def test_update_memory_audits_the_new_id(session: Session, ctx: StubContext, repo: Repo) -> None:
    original = service.add_memory(
        session, ctx, NullEngine(), content="v1", kind="fact", initiated_by="user"
    )
    updated = service.update_memory(session, ctx, NullEngine(), memory_id=original.id, content="v2")

    actions = [(entry.action, entry.target_id) for entry in repo.list_audit()]
    assert actions == [("memory.update", str(updated.id)), ("memory.add", str(original.id))]


def test_update_memory_on_an_unknown_id_is_not_found(session: Session, ctx: StubContext) -> None:
    with pytest.raises(errors.NotFoundError) as caught:
        service.update_memory(session, ctx, NullEngine(), memory_id=uuid.uuid4(), content="nope")
    assert caught.value.code == "NOT_FOUND"


def test_a_superseded_memory_cannot_be_superseded_again(session: Session, ctx: StubContext) -> None:
    """Otherwise the chain forks into two heads and "current" stops being one row."""
    original = service.add_memory(
        session, ctx, NullEngine(), content="v1", kind="fact", initiated_by="user"
    )
    service.update_memory(session, ctx, NullEngine(), memory_id=original.id, content="v2")

    with pytest.raises(errors.NotFoundError):
        service.update_memory(session, ctx, NullEngine(), memory_id=original.id, content="v2-again")


def test_a_tombstoned_memory_cannot_be_updated(session: Session, ctx: StubContext) -> None:
    """C1.4: chains die at a tombstoned head — no resurrection."""
    record = service.add_memory(
        session, ctx, NullEngine(), content="forget me", kind="fact", initiated_by="user"
    )
    service.delete_memory(session, ctx, memory_id=record.id)

    with pytest.raises(errors.NotFoundError):
        service.update_memory(session, ctx, NullEngine(), memory_id=record.id, content="remember")


def _second_workspace(session: Session, user: User) -> StubContext:
    """A second workspace in the same vault, with its own connection."""
    workspace = create_workspace(session, user_id=user.id, name="Work")
    repo = Repo.open(session, workspace.id)
    connection = repo.add_connection(
        client_name="cursor", auth_mode=AuthMode.PAT, writes_enabled=True
    )
    return StubContext(connection_id=connection.id, workspace_id=workspace.id)


def test_update_memory_across_workspaces_is_not_found(
    session: Session, ctx: StubContext, user: User
) -> None:
    """A memory id from another workspace must look exactly like a missing one —
    confirming it exists elsewhere is a cross-workspace leak (C1.8)."""
    other_ctx = _second_workspace(session, user)
    theirs = service.add_memory(
        session, other_ctx, NullEngine(), content="their secret", kind="fact", initiated_by="user"
    )

    with pytest.raises(errors.NotFoundError):
        service.update_memory(session, ctx, NullEngine(), memory_id=theirs.id, content="mine now")


# ---------------------------------------------------------------------------
# delete / tombstone
# ---------------------------------------------------------------------------


def test_delete_memory_tombstones_and_drops_it_from_the_current_view(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    record = service.add_memory(
        session, ctx, NullEngine(), content="forget me", kind="fact", initiated_by="user"
    )
    service.delete_memory(session, ctx, memory_id=record.id)

    stored = repo.get_memory(record.id)
    assert stored is not None, "the row survives; only the flag flips"
    assert stored.tombstone is True
    assert stored.content == "forget me"
    assert repo.current_memories() == []


def test_delete_memory_is_idempotent(session: Session, ctx: StubContext, repo: Repo) -> None:
    record = service.add_memory(
        session, ctx, NullEngine(), content="forget me", kind="fact", initiated_by="user"
    )
    service.delete_memory(session, ctx, memory_id=record.id)
    service.delete_memory(session, ctx, memory_id=record.id)  # must not raise

    deletes = [entry for entry in repo.list_audit() if entry.action == "memory.delete"]
    assert len(deletes) == 2, "a retried delete is still an auditable request"


def test_delete_memory_audits(session: Session, ctx: StubContext, repo: Repo) -> None:
    record = service.add_memory(
        session, ctx, NullEngine(), content="forget me", kind="fact", initiated_by="user"
    )
    service.delete_memory(session, ctx, memory_id=record.id)
    latest = repo.list_audit()[0]
    assert latest.action == "memory.delete"
    assert latest.target_id == str(record.id)


def test_delete_memory_on_an_unknown_id_is_not_found(session: Session, ctx: StubContext) -> None:
    with pytest.raises(errors.NotFoundError) as caught:
        service.delete_memory(session, ctx, memory_id=uuid.uuid4())
    assert caught.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_falls_back_to_ilike_when_the_engine_has_nothing(
    session: Session, ctx: StubContext
) -> None:
    """The M1 smoke path: no engine, but add→search still round-trips."""
    wanted = service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="I prefer TypeScript.",
        kind="preference",
        initiated_by="user",
    )
    service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="Deploys go out on Thursdays.",
        kind="decision",
        initiated_by="user",
    )

    hits = service.search_memory(session, ctx, NullEngine(), query="typescript")
    assert [hit.id for hit in hits] == [wanted.id]
    assert hits[0].content == "I prefer TypeScript."
    assert hits[0].kind is MemoryKind.PREFERENCE
    assert hits[0].score is None, "the fallback has no notion of relevance"
    assert hits[0].provenance.connection_id == ctx.connection_id
    assert hits[0].provenance.initiated_by is InitiatedBy.USER


def test_fallback_search_is_case_insensitive_and_substring(
    session: Session, ctx: StubContext
) -> None:
    record = service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="I prefer TypeScript.",
        kind="preference",
        initiated_by="user",
    )
    for query in ("typescript", "TYPESCRIPT", "peScri"):
        assert [
            hit.id for hit in service.search_memory(session, ctx, NullEngine(), query=query)
        ] == [record.id]


def test_fallback_search_is_not_semantic(session: Session, ctx: StubContext) -> None:
    """Documented honestly rather than wished away: this is why C3.4/C3.5 exist."""
    service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="I prefer TypeScript.",
        kind="preference",
        initiated_by="user",
    )
    assert service.search_memory(session, ctx, NullEngine(), query="programming language") == []


def test_fallback_search_treats_wildcards_literally(session: Session, ctx: StubContext) -> None:
    """Un-escaped, ``%`` would match every memory in the workspace."""
    match = service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="Coverage must stay above 80%.",
        kind="decision",
        initiated_by="user",
    )
    service.add_memory(
        session,
        ctx,
        NullEngine(),
        content="Nothing to do with coverage.",
        kind="fact",
        initiated_by="user",
    )
    assert [hit.id for hit in service.search_memory(session, ctx, NullEngine(), query="80%")] == [
        match.id
    ]
    assert service.search_memory(session, ctx, NullEngine(), query="%") == []


def test_search_excludes_tombstoned_and_superseded_memories(
    session: Session, ctx: StubContext
) -> None:
    deleted = service.add_memory(
        session, ctx, NullEngine(), content="tombstoned needle", kind="fact", initiated_by="user"
    )
    service.delete_memory(session, ctx, memory_id=deleted.id)

    old = service.add_memory(
        session, ctx, NullEngine(), content="superseded needle", kind="fact", initiated_by="user"
    )
    new = service.update_memory(
        session, ctx, NullEngine(), memory_id=old.id, content="current needle"
    )

    hits = service.search_memory(session, ctx, NullEngine(), query="needle")
    assert [hit.id for hit in hits] == [new.id]


def test_search_respects_the_limit(session: Session, ctx: StubContext) -> None:
    for index in range(5):
        service.add_memory(
            session,
            ctx,
            NullEngine(),
            content=f"needle {index}",
            kind="fact",
            initiated_by="user",
        )
    assert len(service.search_memory(session, ctx, NullEngine(), query="needle", limit=2)) == 2


def test_engine_hits_are_hydrated_from_postgres_and_keep_the_engine_ranking(
    session: Session, ctx: StubContext
) -> None:
    """The index proposes; Postgres disposes. The returned content is canonical."""
    first = service.add_memory(
        session, ctx, NullEngine(), content="alpha", kind="fact", initiated_by="user"
    )
    second = service.add_memory(
        session, ctx, NullEngine(), content="beta", kind="fact", initiated_by="user"
    )

    engine = RecordingEngine(
        hits=[
            EngineHit(memory_id=second.id, score=0.9, content="mem0's rewritten phrasing"),
            EngineHit(memory_id=first.id, score=0.4),
        ]
    )
    hits = service.search_memory(session, ctx, engine, query="anything")

    assert [hit.id for hit in hits] == [second.id, first.id], "engine order preserved"
    assert [hit.content for hit in hits] == ["beta", "alpha"], "content comes from canonical rows"
    assert [hit.score for hit in hits] == [0.9, 0.4]


def test_a_stale_engine_cannot_resurrect_a_tombstoned_memory(
    session: Session, ctx: StubContext
) -> None:
    """The reason hits are hydrated instead of returned as-is."""
    live = service.add_memory(
        session, ctx, NullEngine(), content="still here", kind="fact", initiated_by="user"
    )
    gone = service.add_memory(
        session, ctx, NullEngine(), content="deleted", kind="fact", initiated_by="user"
    )
    service.delete_memory(session, ctx, memory_id=gone.id)

    engine = RecordingEngine(
        hits=[EngineHit(memory_id=gone.id, score=1.0), EngineHit(memory_id=live.id, score=0.1)]
    )
    hits = service.search_memory(session, ctx, engine, query="anything")
    assert [hit.id for hit in hits] == [live.id]


def test_an_engine_that_only_returns_dead_ids_falls_back(
    session: Session, ctx: StubContext
) -> None:
    record = service.add_memory(
        session, ctx, NullEngine(), content="findable needle", kind="fact", initiated_by="user"
    )
    engine = RecordingEngine(hits=[EngineHit(memory_id=uuid.uuid4(), score=1.0)])

    hits = service.search_memory(session, ctx, engine, query="needle")
    assert [hit.id for hit in hits] == [record.id]


def test_search_does_not_cross_workspaces(session: Session, ctx: StubContext, user: User) -> None:
    other_ctx = _second_workspace(session, user)
    service.add_memory(
        session, other_ctx, NullEngine(), content="their needle", kind="fact", initiated_by="user"
    )

    assert service.search_memory(session, ctx, NullEngine(), query="needle") == []
    assert service.list_memories(session, ctx).items == []


# ---------------------------------------------------------------------------
# list / pagination
# ---------------------------------------------------------------------------


def test_list_memories_returns_current_memories_newest_first(
    session: Session, ctx: StubContext
) -> None:
    ids = [
        service.add_memory(
            session,
            ctx,
            NullEngine(),
            content=f"memory {index}",
            kind="fact",
            initiated_by="user",
        ).id
        for index in range(3)
    ]
    page = service.list_memories(session, ctx)
    assert {item.id for item in page.items} == set(ids)
    assert page.next_cursor is None


def test_list_memories_excludes_tombstoned_and_superseded(
    session: Session, ctx: StubContext
) -> None:
    kept = service.add_memory(
        session, ctx, NullEngine(), content="kept", kind="fact", initiated_by="user"
    )
    dropped = service.add_memory(
        session, ctx, NullEngine(), content="dropped", kind="fact", initiated_by="user"
    )
    service.delete_memory(session, ctx, memory_id=dropped.id)
    old = service.add_memory(
        session, ctx, NullEngine(), content="old", kind="fact", initiated_by="user"
    )
    new = service.update_memory(session, ctx, NullEngine(), memory_id=old.id, content="new")

    page = service.list_memories(session, ctx)
    assert {item.id for item in page.items} == {kept.id, new.id}


def test_list_memories_pages_across_three_pages_without_gaps_or_repeats(
    session: Session, ctx: StubContext
) -> None:
    """Six memories, two per page. The property that matters is that the union of
    the pages is exactly the set of memories: no row seen twice, none skipped.

    All six share a ``created_at`` (one transaction, one ``now()``), so this is
    the *hard* case for the cursor — ordering is decided entirely by the id
    tiebreak. Get that wrong and this test sees duplicates.
    """
    written = {
        service.add_memory(
            session,
            ctx,
            NullEngine(),
            content=f"memory {index}",
            kind="fact",
            initiated_by="user",
        ).id
        for index in range(6)
    }

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for expected_page in range(3):
        page = service.list_memories(session, ctx, cursor=cursor, limit=2)
        assert len(page.items) == 2, f"page {expected_page} was short"
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor

    assert cursor is None, "the third page is the last one"
    assert len(seen) == len(set(seen)), "a row was returned on two different pages"
    assert set(seen) == written


def test_the_last_page_has_no_cursor_even_when_exactly_full(
    session: Session, ctx: StubContext
) -> None:
    """The off-by-one that makes a client fetch an empty page forever."""
    for index in range(4):
        service.add_memory(
            session,
            ctx,
            NullEngine(),
            content=f"memory {index}",
            kind="fact",
            initiated_by="user",
        )

    first = service.list_memories(session, ctx, limit=2)
    assert first.next_cursor is not None
    second = service.list_memories(session, ctx, cursor=first.next_cursor, limit=2)
    assert len(second.items) == 2
    assert second.next_cursor is None


def test_list_memories_on_an_empty_workspace(session: Session, ctx: StubContext) -> None:
    page = service.list_memories(session, ctx)
    assert page.items == []
    assert page.next_cursor is None


def test_a_forged_cursor_is_a_validation_error(session: Session, ctx: StubContext) -> None:
    with pytest.raises(errors.ValidationError):
        service.list_memories(session, ctx, cursor="obviously-not-a-cursor")
