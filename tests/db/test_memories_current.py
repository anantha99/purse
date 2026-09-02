"""C1.4: what ``memories_current`` says is current.

The rule under test: a memory is current when it is not tombstoned and nothing
has ever superseded it. Supersession is permanent — the successor's own fate
does not hand the predecessor its life back.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from purse.db.models import Connection, InitiatedBy, Memory, MemoryKind
from purse.db.repo import Repo

pytestmark = pytest.mark.db


def _add(repo: Repo, connection: Connection, content: str) -> Memory:
    return repo.add_memory(
        content=content,
        kind=MemoryKind.FACT,
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )


def _current(repo: Repo) -> set[str]:
    return {row.content for row in repo.current_memories()}


def test_a_fresh_memory_is_current(session: Session, repo: Repo, connection: Connection) -> None:
    _add(repo, connection, "lives in Bengaluru")
    session.flush()
    assert _current(repo) == {"lives in Bengaluru"}


def test_a_tombstoned_memory_is_not_current(
    session: Session, repo: Repo, connection: Connection
) -> None:
    memory = _add(repo, connection, "temporary note")
    repo.tombstone_memory(memory.id)
    session.flush()
    assert _current(repo) == set()
    # ...but it is still in the log. That is the point of a tombstone.
    assert len(repo.list_memories()) == 1


def test_only_the_head_of_a_supersession_chain_is_current(
    session: Session, repo: Repo, connection: Connection
) -> None:
    """A <- B <- C: only C."""
    a = repo.add_memory(
        content="A",
        kind=MemoryKind.FACT,
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )
    b = repo.supersede_memory(
        a.id, content="B", connection_id=connection.id, initiated_by=InitiatedBy.USER
    )
    repo.supersede_memory(
        b.id, content="C", connection_id=connection.id, initiated_by=InitiatedBy.USER
    )
    session.flush()

    assert _current(repo) == {"C"}
    assert len(repo.list_memories()) == 3


def test_tombstoning_the_head_kills_the_whole_chain(
    session: Session, repo: Repo, connection: Connection
) -> None:
    """A <- B <- C, C tombstoned: nothing is current. B and A do NOT resurrect.

    This is the bug the "superseded by a *live* row" reading would introduce:
    deleting the latest version of a note would silently republish an older
    one. Supersession is a permanent fact about the predecessor.
    """
    a = repo.add_memory(
        content="A",
        kind=MemoryKind.FACT,
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )
    b = repo.supersede_memory(
        a.id, content="B", connection_id=connection.id, initiated_by=InitiatedBy.USER
    )
    c = repo.supersede_memory(
        b.id, content="C", connection_id=connection.id, initiated_by=InitiatedBy.USER
    )
    session.flush()

    assert repo.tombstone_memory(c.id) is True
    session.flush()

    assert _current(repo) == set()
    # The history survives in full — this is what an export carries.
    assert {m.content for m in repo.list_memories()} == {"A", "B", "C"}


def test_tombstoning_a_middle_link_does_not_change_the_head(
    session: Session, repo: Repo, connection: Connection
) -> None:
    a = repo.add_memory(
        content="A",
        kind=MemoryKind.FACT,
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )
    b = repo.supersede_memory(
        a.id, content="B", connection_id=connection.id, initiated_by=InitiatedBy.USER
    )
    repo.supersede_memory(
        b.id, content="C", connection_id=connection.id, initiated_by=InitiatedBy.USER
    )
    session.flush()
    repo.tombstone_memory(b.id)
    session.flush()
    assert _current(repo) == {"C"}


def test_two_chains_are_independent(session: Session, repo: Repo, connection: Connection) -> None:
    first = _add(repo, connection, "chain one v1")
    repo.supersede_memory(
        first.id,
        content="chain one v2",
        connection_id=connection.id,
        initiated_by=InitiatedBy.USER,
    )
    _add(repo, connection, "standalone")
    session.flush()
    assert _current(repo) == {"chain one v2", "standalone"}
