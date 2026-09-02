"""C1.3: the append-only guarantee is Postgres's, not Python's.

Every mutation below is attempted with raw SQL, deliberately bypassing the
repository. If the only thing stopping an UPDATE were application code, these
tests would pass while the guarantee was worthless.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from purse.db.models import EMBEDDING_DIM, Connection, InitiatedBy, Memory, MemoryKind
from purse.db.repo import Repo

pytestmark = pytest.mark.db


def _memory(repo: Repo, connection: Connection, content: str = "prefers TypeScript") -> Memory:
    return repo.add_memory(
        content=content,
        kind=MemoryKind.PREFERENCE,
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("content", "'rewritten behind the user''s back'"),
        ("kind", "'fact'::memory_kind"),
        ("initiated_by", "'agent'::memory_initiator"),
        ("agent_id", "'some-other-agent'"),
        ("created_at", "now() - interval '10 years'"),
        ("id", "gen_random_uuid()"),
    ],
)
def test_updating_an_immutable_column_is_rejected(
    session: Session, repo: Repo, connection: Connection, column: str, value: str
) -> None:
    memory = _memory(repo, connection)
    session.flush()

    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            text(f"UPDATE memories SET {column} = {value} WHERE id = :id"),  # noqa: S608
            {"id": memory.id},
        )
    assert "append-only" in str(caught.value)
    assert column in str(caught.value)


def test_updating_workspace_id_is_rejected(
    session: Session, repo: Repo, connection: Connection
) -> None:
    """Moving a memory between workspaces would be an isolation break."""
    memory = _memory(repo, connection)
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            text("UPDATE memories SET workspace_id = :other WHERE id = :id"),
            {"other": uuid.uuid4(), "id": memory.id},
        )
    assert "append-only" in str(caught.value)


def test_deleting_a_memory_is_rejected(
    session: Session, repo: Repo, connection: Connection
) -> None:
    memory = _memory(repo, connection)
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(text("DELETE FROM memories WHERE id = :id"), {"id": memory.id})
    assert "may not be DELETEd" in str(caught.value)


def test_truncating_memories_is_rejected(
    session: Session, repo: Repo, connection: Connection
) -> None:
    _memory(repo, connection)
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(text("TRUNCATE memories"))
    assert "may not be TRUNCATEd" in str(caught.value)


def test_tombstone_may_be_set(session: Session, repo: Repo, connection: Connection) -> None:
    """The one permitted flip on the canonical columns."""
    memory = _memory(repo, connection)
    assert repo.tombstone_memory(memory.id) is True
    session.flush()
    stored = repo.get_memory(memory.id)
    assert stored is not None
    assert stored.tombstone is True
    # A second attempt is a no-op, not an error.
    assert repo.tombstone_memory(memory.id) is False


def test_tombstone_may_not_be_cleared(session: Session, repo: Repo, connection: Connection) -> None:
    memory = _memory(repo, connection)
    repo.tombstone_memory(memory.id)
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            text("UPDATE memories SET tombstone = false WHERE id = :id"), {"id": memory.id}
        )
    assert "only be set, never cleared" in str(caught.value)


def test_tombstoning_together_with_a_content_edit_is_rejected(
    session: Session, repo: Repo, connection: Connection
) -> None:
    """The permitted flip is not a loophole to smuggle an edit through."""
    memory = _memory(repo, connection)
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            text("UPDATE memories SET tombstone = true, content = 'edited' WHERE id = :id"),
            {"id": memory.id},
        )
    assert "content" in str(caught.value)


def test_supersedes_may_not_be_cleared(
    session: Session, repo: Repo, connection: Connection
) -> None:
    """Rewriting history by detaching a successor from its predecessor."""
    original = _memory(repo, connection, "prefers tabs")
    replacement = repo.supersede_memory(
        original.id,
        content="prefers spaces",
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            text("UPDATE memories SET supersedes = NULL WHERE id = :id"), {"id": replacement.id}
        )
    assert "supersedes" in str(caught.value)


def test_embedding_may_be_written_because_it_is_derived(
    session: Session, repo: Repo, connection: Connection
) -> None:
    """C3.6 requires the derived index to be rebuildable in place.

    An embedding carries no information that is not already in ``content``, so
    it sits outside the immutability contract by design.
    """
    memory = _memory(repo, connection)
    assert memory.embedding is None
    assert repo.set_embedding(memory.id, [0.5] * EMBEDDING_DIM) is True
    session.flush()
    stored = repo.get_memory(memory.id)
    assert stored is not None
    assert stored.embedding is not None
    assert len(stored.embedding) == EMBEDDING_DIM
    # And rewriting it (a rebuild) is fine too.
    assert repo.set_embedding(memory.id, [0.25] * EMBEDDING_DIM) is True


def test_a_memory_cannot_supersede_itself(
    session: Session, repo: Repo, connection: Connection
) -> None:
    memory = _memory(repo, connection)
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            text("UPDATE memories SET supersedes = id WHERE id = :id"), {"id": memory.id}
        )
    # Rejected by the trigger before the CHECK constraint even gets a look in.
    assert "append-only" in str(caught.value)


def test_supersession_writes_a_new_row_and_leaves_the_old_one_intact(
    session: Session, repo: Repo, connection: Connection
) -> None:
    original = _memory(repo, connection, "prefers tabs")
    replacement = repo.supersede_memory(
        original.id,
        content="prefers spaces",
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )
    session.flush()

    assert replacement.supersedes == original.id
    assert replacement.kind == original.kind
    still_there = repo.get_memory(original.id)
    assert still_there is not None
    assert still_there.content == "prefers tabs"
    assert still_there.tombstone is False
    assert len(repo.list_memories()) == 2
