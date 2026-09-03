"""Read projections the memories screen needs, beyond the memory service (C7.2).

The agent-facing ``purse.memory.service`` returns bare :class:`MemoryRecord`
pages. The dashboard needs three things the service does not provide, and none
of them are the write path (which stays entirely in the service):

* **client_name resolved** — the current-view item carries the display name of
  the connection that wrote it, not just its id.
* **superseded_count** — how many prior versions sit behind a current memory,
  so the UI can show an "edited 3x" badge and offer the history view.
* **kind / initiated_by filters + keyset pagination** — the contract's
  ``GET /web/memories`` filters the current view, which
  ``service.list_memories`` cannot. This module issues its own workspace-scoped
  keyset query over ``memories_current`` — the same pattern the memory service
  itself uses internally (typed selects over the mapped view, every one carrying
  the ``workspace_id`` predicate explicitly), promoted here because a filtered
  read is a projection, not business logic.

The supersession *history* chain (:func:`history_chain`) is likewise a read over
the canonical log via the workspace-scoped :class:`Repo`.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from purse.db.models import InitiatedBy, Memory, MemoryCurrent, MemoryKind
from purse.db.repo import Repo
from purse.memory.records import MemoryRecord
from purse.memory.service import DEFAULT_LIST_LIMIT, MAX_LIMIT
from purse.web.errors import ValidationError

__all__ = [
    "CurrentPage",
    "history_chain",
    "history_entry",
    "item_dict",
    "list_current",
    "superseded_count",
]


@dataclass(frozen=True, slots=True)
class CurrentPage:
    """One page of current memories (as rows) plus the keyset cursor for the next."""

    rows: Sequence[MemoryCurrent]
    next_cursor: str | None


def _encode_cursor(created_at: dt.datetime, memory_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{memory_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        timestamp, _, memory_id = raw.rpartition("|")
        return dt.datetime.fromisoformat(timestamp), uuid.UUID(memory_id)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationError("cursor is not a cursor this server issued") from exc


def _validate_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIST_LIMIT
    if limit < 1:
        raise ValidationError("limit must be at least 1")
    return min(limit, MAX_LIMIT)


def _parse_kind(kind: str | None) -> MemoryKind | None:
    if kind is None or kind == "":
        return None
    try:
        return MemoryKind(kind)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in MemoryKind)
        raise ValidationError(f"kind must be one of: {allowed}") from exc


def _parse_initiated_by(initiated_by: str | None) -> InitiatedBy | None:
    if initiated_by is None or initiated_by == "":
        return None
    try:
        return InitiatedBy(initiated_by)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in InitiatedBy)
        raise ValidationError(f"initiated_by must be one of: {allowed}") from exc


def list_current(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int | None = None,
    kind: str | None = None,
    initiated_by: str | None = None,
) -> CurrentPage:
    """A keyset page of the current view, newest first, with optional filters.

    The key is ``(created_at, id)`` descending — identical to
    ``service.list_memories`` — so cursors mean the same thing and ties are
    ordered stably. ``kind`` / ``initiated_by`` narrow the view before
    pagination, so a page is never short because a filter dropped rows.
    """
    checked_limit = _validate_limit(limit)
    checked_kind = _parse_kind(kind)
    checked_initiator = _parse_initiated_by(initiated_by)
    after = _decode_cursor(cursor) if cursor else None
    Repo.open(session, workspace_id)  # existence + isolation check

    stmt = (
        select(MemoryCurrent)
        .where(MemoryCurrent.workspace_id == workspace_id)
        .order_by(MemoryCurrent.created_at.desc(), MemoryCurrent.id.desc())
        .limit(checked_limit + 1)
    )
    if checked_kind is not None:
        stmt = stmt.where(MemoryCurrent.kind == checked_kind)
    if checked_initiator is not None:
        stmt = stmt.where(MemoryCurrent.initiated_by == checked_initiator)
    if after is not None:
        stmt = stmt.where(tuple_(MemoryCurrent.created_at, MemoryCurrent.id) < after)

    rows = list(session.scalars(stmt))
    has_more = len(rows) > checked_limit
    page = rows[:checked_limit]
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    return CurrentPage(rows=page, next_cursor=next_cursor)


def superseded_count(repo: Repo, supersedes: uuid.UUID | None) -> int:
    """How many prior versions sit behind a memory, walking ``supersedes`` back.

    Supersession chains are linear (only a *current* memory can be superseded,
    so a head never forks), which bounds this walk at the chain length — one or
    two rows in practice.
    """
    count = 0
    seen: set[uuid.UUID] = set()
    current = supersedes
    while current is not None and current not in seen:
        seen.add(current)
        row = repo.get_memory(current)
        if row is None:
            break
        count += 1
        current = row.supersedes
    return count


def item_dict(record: MemoryRecord, *, client_name: str | None, superseded: int) -> dict[str, Any]:
    """The current-view item shape from the contract (also reused for search hits)."""
    return {
        "id": str(record.id),
        "content": record.content,
        "kind": record.kind.value,
        "created_at": record.created_at.isoformat(),
        "provenance": {
            "connection_id": str(record.provenance.connection_id),
            "client_name": client_name,
            "agent_id": record.provenance.agent_id,
            "initiated_by": record.provenance.initiated_by.value,
        },
        "superseded_count": superseded,
    }


def history_chain(repo: Repo, memory_id: uuid.UUID) -> list[Memory] | None:
    """The full supersession chain a memory belongs to, oldest → newest.

    Returns ``None`` when the id is not in this workspace. The given id may be
    any version in the chain — an old superseded row or the current head — and
    the whole chain comes back either way.
    """
    rows = repo.list_memories()  # full canonical log for this workspace
    by_id = {row.id: row for row in rows}
    if memory_id not in by_id:
        return None

    # Walk back to the root.
    node = by_id[memory_id]
    seen: set[uuid.UUID] = set()
    while node.supersedes is not None and node.supersedes in by_id and node.supersedes not in seen:
        seen.add(node.id)
        node = by_id[node.supersedes]
    root = node

    # Walk forward: each row is superseded by at most one successor.
    successor = {row.supersedes: row for row in rows if row.supersedes is not None}
    chain = [root]
    walked: set[uuid.UUID] = {root.id}
    current = root
    while current.id in successor and successor[current.id].id not in walked:
        current = successor[current.id]
        chain.append(current)
        walked.add(current.id)
    return chain


def history_entry(memory: Memory, *, client_name: str | None) -> dict[str, Any]:
    """One version in a history response: ``{id,content,created_at,provenance,tombstoned}``."""
    return {
        "id": str(memory.id),
        "content": memory.content,
        "created_at": memory.created_at.isoformat(),
        "provenance": {
            "connection_id": str(memory.connection_id),
            "client_name": client_name,
            "agent_id": memory.agent_id,
            "initiated_by": memory.initiated_by.value,
        },
        "tombstoned": memory.tombstone,
    }
