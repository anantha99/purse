"""The canonical memory write and read path (PRD §8.2, §12; C3.1-C3.2).

PRD §12 states the write path as a sentence: **auth → scope → canonical insert
(sync) → engine ingest (async) → audit.** This module owns the last three steps.
Auth and scope belong to C2 and are already done by the time a
:class:`~purse.memory.context.WriteContext` reaches these functions — that object
*is* the proof.

Three properties hold across everything below.

**Canonical first, and canonical alone decides success.** The Postgres insert is
synchronous and its transaction is what the caller's success depends on. Engine
work happens after, wrapped in ``try``: an index that is down degrades recall,
never durability (see :mod:`purse.memory.engine`).

**Nothing mutates.** ``update_memory`` appends a successor row pointing at its
predecessor; ``delete_memory`` flips a tombstone flag. The ``memories`` table's
triggers reject anything else, so this is not a convention that a future
contributor can quietly break (C1.3).

**Every write is audited.** ``memory.add`` / ``memory.update`` / ``memory.delete``
with the affected id as the target. Names and IDs only, never content (PRD §13).

.. rubric:: On transactions

These functions ``flush`` but never ``commit``. The caller owns the transaction
boundary — the REST gateway commits per request, tests roll back, and a future
batch importer can wrap many writes in one. That also means the audit row and
the memory row land atomically: you cannot get one without the other.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from purse.db.models import InitiatedBy, MemoryCurrent, MemoryKind
from purse.db.repo import NotFoundError as DbNotFoundError
from purse.db.repo import Repo
from purse.memory.context import WriteContext
from purse.memory.engine import EngineHit, MemoryEngine
from purse.memory.errors import NotFoundError, PayloadTooLargeError, ValidationError
from purse.memory.records import MemoryRecord, SearchHit

__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_CONTENT_BYTES",
    "MAX_LIMIT",
    "MemoryPage",
    "add_memory",
    "delete_memory",
    "list_memories",
    "search_memory",
    "update_memory",
]

logger = logging.getLogger(__name__)

#: PRD §10: ``add_memory`` content is capped at 4 KB. Measured in **UTF-8 bytes**,
#: not characters — "4 KB" is a storage and transport claim, and a string of
#: 4096 emoji is 16 KB on the wire. A caller that counts characters will
#: occasionally be surprised; that is the correct surprise to have.
MAX_CONTENT_BYTES = 4096

#: PRD §10 gives ``search_memory`` a default limit of 8.
DEFAULT_SEARCH_LIMIT = 8
DEFAULT_LIST_LIMIT = 50
#: Upper bound on any page. Protects the gateway from a caller asking for the
#: whole vault in one response.
MAX_LIMIT = 200

_AUDIT_TARGET = "memory"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryPage:
    """One page of current memories, newest first.

    ``next_cursor`` is ``None`` exactly when this is the last page.
    """

    items: Sequence[MemoryRecord]
    next_cursor: str | None


def _encode_cursor(created_at: dt.datetime, memory_id: uuid.UUID) -> str:
    """Opaque keyset cursor over ``(created_at, id)``.

    Base64 of ``<iso8601>|<uuid>``. Opaque so the shape can change without
    breaking clients that stored one; base64 rather than JSON so nobody is
    tempted to treat it as a filter they can edit.
    """
    raw = f"{created_at.isoformat()}|{memory_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        timestamp, _, memory_id = raw.rpartition("|")
        return dt.datetime.fromisoformat(timestamp), uuid.UUID(memory_id)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationError("cursor is not a cursor this server issued") from exc


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_content(content: str) -> str:
    """Non-empty, and ≤ :data:`MAX_CONTENT_BYTES` UTF-8 bytes.

    Content is stored **verbatim** (PRD §8.2), so this only rejects — it never
    trims, normalises, or truncates. Truncating a memory to fit would store
    something the user never said, which is worse than an error.
    """
    if not isinstance(content, str):  # pragma: no cover - typed, but callers cross a wire
        raise ValidationError("content must be a string")
    if not content.strip():
        raise ValidationError("content must not be empty")
    size = len(content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise PayloadTooLargeError(
            f"content is {size} bytes; the limit is {MAX_CONTENT_BYTES} bytes of UTF-8"
        )
    return content


def _validate_kind(kind: MemoryKind | str) -> MemoryKind:
    if isinstance(kind, MemoryKind):
        return kind
    try:
        return MemoryKind(kind)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in MemoryKind)
        raise ValidationError(f"kind must be one of: {allowed}") from exc


def _validate_initiated_by(initiated_by: InitiatedBy | str) -> InitiatedBy:
    if isinstance(initiated_by, InitiatedBy):
        return initiated_by
    try:
        return InitiatedBy(initiated_by)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in InitiatedBy)
        raise ValidationError(f"initiated_by must be one of: {allowed}") from exc


def _validate_limit(limit: int | None, *, default: int) -> int:
    """Clamp rather than reject an over-large limit; reject a nonsensical one.

    A caller asking for 10 000 results wants "as many as you'll give me", and
    :data:`MAX_LIMIT` of them is a useful answer. A caller asking for zero or a
    negative number has a bug, and silently returning an empty page would hide
    it.
    """
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValidationError("limit must be an integer") from exc
    if value < 1:
        raise ValidationError("limit must be at least 1")
    return min(value, MAX_LIMIT)


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValidationError("query must not be empty")
    return query


def _open_repo(session: Session, ctx: WriteContext) -> Repo:
    """The workspace-bound repository for this caller.

    ``Repo`` takes the workspace at construction and no method on it accepts one
    (C1.8), so from here on the isolation boundary is structural.
    """
    try:
        return Repo.open(session, ctx.workspace_id)
    except DbNotFoundError as exc:
        # A context pointing at a workspace that does not exist means the auth
        # layer authenticated against a deleted vault. Surfacing NOT_FOUND keeps
        # the response shape honest without leaking which half is missing.
        raise NotFoundError("workspace not found") from exc


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def add_memory(
    session: Session,
    ctx: WriteContext,
    engine: MemoryEngine,
    *,
    content: str,
    kind: MemoryKind | str,
    initiated_by: InitiatedBy | str,
) -> MemoryRecord:
    """Append a verbatim canonical memory, then audit, then index (PRD §8.2, C3.1).

    Raises :class:`~purse.memory.errors.ValidationError` /
    :class:`~purse.memory.errors.PayloadTooLargeError` before touching the
    database. Engine failures are logged and swallowed — the returned record is
    already durable in this session's transaction.
    """
    checked_content = _validate_content(content)
    checked_kind = _validate_kind(kind)
    checked_initiator = _validate_initiated_by(initiated_by)

    repo = _open_repo(session, ctx)
    row = repo.add_memory(
        content=checked_content,
        kind=checked_kind,
        connection_id=ctx.connection_id,
        initiated_by=checked_initiator,
        agent_id=ctx.agent_id,
    )
    record = MemoryRecord.from_model(row)
    _audit(repo, ctx, action="memory.add", target_id=record.id)
    _ingest(engine, record, ctx.workspace_id)
    return record


def update_memory(
    session: Session,
    ctx: WriteContext,
    engine: MemoryEngine,
    *,
    memory_id: uuid.UUID,
    content: str,
    initiated_by: InitiatedBy | str = InitiatedBy.USER,
) -> MemoryRecord:
    """Supersede a current memory with new content (C3.2).

    The old row is untouched — it is the history. The returned record is the
    *new* id, which is what PRD §10 says ``update_memory`` returns.

    Only a **current** memory can be superseded. Superseding an already-superseded
    row would fork the chain into two heads, and tombstoned chains must stay dead
    (C1.4: "chains die at a tombstoned head — no resurrection"). Both cases are
    ``NOT_FOUND``: from the caller's point of view that id is not a memory they
    can edit.

    PRD §10 gives ``update_memory`` only ``id`` and ``content``, so
    ``initiated_by`` defaults to ``user`` — editing a stored fact is a deliberate
    act, not an incidental one. A caller that knows an agent drove the edit
    should say so; like every ``initiated_by`` it is a claim, and the trusted
    provenance is the connection.
    """
    checked_content = _validate_content(content)
    checked_initiator = _validate_initiated_by(initiated_by)
    repo = _open_repo(session, ctx)
    if _get_current(session, ctx.workspace_id, memory_id) is None:
        raise NotFoundError(f"memory {memory_id} is not a current memory in this workspace")

    row = repo.supersede_memory(
        memory_id,
        content=checked_content,
        connection_id=ctx.connection_id,
        initiated_by=checked_initiator,
        agent_id=ctx.agent_id,
    )
    record = MemoryRecord.from_model(row)
    _audit(repo, ctx, action="memory.update", target_id=record.id)
    _ingest(engine, record, ctx.workspace_id)
    return record


def delete_memory(session: Session, ctx: WriteContext, *, memory_id: uuid.UUID) -> None:
    """Tombstone a memory (C3.2). Idempotent.

    The row survives — this is a flag, not a delete, and the database refuses to
    un-set it. Calling twice is not an error: the second call finds the memory
    already tombstoned and returns, because "make sure this is gone" is a
    request that should be safe to retry.

    Unknown ids are ``NOT_FOUND``. Note this looks in the whole canonical log,
    not the current view, so deleting an already-superseded row is allowed —
    tombstoning history is a legitimate erasure request.

    Takes no engine: removal from the index is C3.4's business (the engine is
    rebuilt from the current view, which no longer contains this row), and there
    is no engine call whose failure could be misread as the delete failing.
    """
    repo = _open_repo(session, ctx)
    if repo.get_memory(memory_id) is None:
        raise NotFoundError(f"memory {memory_id} not found in this workspace")
    repo.tombstone_memory(memory_id)
    _audit(repo, ctx, action="memory.delete", target_id=memory_id)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def search_memory(
    session: Session,
    ctx: WriteContext,
    engine: MemoryEngine,
    *,
    query: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[SearchHit]:
    """Ranked current memories for *query* — engine first, canonical fallback.

    **The engine is asked first.** When one is installed (Mem0, C3.4; pgvector,
    C3.5) this is semantic recall: its hits are ids, which are then hydrated from
    Postgres and filtered to the current view. That hydration is not ceremony —
    it is what stops a stale index from resurrecting a tombstoned memory or
    showing Mem0's rewritten phrasing as if the vault contained it.

    **The fallback is a substring match.** With the M1 default
    (:class:`~purse.memory.engine.NullEngine`) there is no index, so this is a
    Postgres ``ILIKE`` over current content, newest first. It finds nothing a
    human would call "semantic" — searching "vim" will not surface "I prefer
    modal editors". That is the honest M1 smoke path: it proves the write→read
    round trip end to end while the real engine lands behind the same signature,
    with no call-site change.

    An engine that raises is treated as an engine that found nothing.
    """
    checked_query = _validate_query(query)
    checked_limit = _validate_limit(limit, default=DEFAULT_SEARCH_LIMIT)
    _open_repo(session, ctx)  # existence + isolation check, same as every other entry point

    hits = _engine_search(engine, ctx.workspace_id, checked_query, checked_limit)
    if hits:
        scores = {hit.memory_id: hit.score for hit in hits}
        rows = _get_current_many(session, ctx.workspace_id, list(scores))
        # Preserve the engine's ranking; drop ids it knew about that are no
        # longer current.
        by_id = {row.id: row for row in rows}
        ranked = [
            SearchHit.from_record(MemoryRecord.from_model(by_id[hit.memory_id]), score=hit.score)
            for hit in hits
            if hit.memory_id in by_id
        ]
        if ranked:
            return ranked[:checked_limit]

    return _fallback_search(session, ctx.workspace_id, checked_query, checked_limit)


def list_memories(
    session: Session,
    ctx: WriteContext,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> MemoryPage:
    """A page of current memories, newest first, keyset-paginated (PRD §10).

    The key is ``(created_at, id)`` descending, and the extra ``id`` is not
    decoration. ``created_at`` defaults to ``now()``, which in Postgres is
    *transaction* time: every row written in one transaction shares a timestamp
    to the microsecond. Ordering by timestamp alone would make their relative
    order a coin flip, and a keyset cursor built on a coin flip skips and repeats
    rows across pages. This is the same trap the audit log hit — it solved it
    with a monotonic ``seq`` column, which ``memories`` does not have.

    Including ``id`` in the key makes the ordering *total* and therefore the
    pagination stable, at the cost of ties being ordered by a random uuid rather
    than by insertion. For real traffic that cost is theoretical: writes arrive
    one per transaction, so timestamps do not tie. Bulk import (and any future
    batch path) can produce a page whose internal order is arbitrary — stable
    across requests, just not chronological within the tied group.
    """
    # Every input is validated before the first query: a bad cursor is a client
    # error, and answering it with a database round trip (or a stack trace) is
    # not an improvement.
    checked_limit = _validate_limit(limit, default=DEFAULT_LIST_LIMIT)
    after = _decode_cursor(cursor) if cursor is not None else None
    _open_repo(session, ctx)

    stmt = (
        select(MemoryCurrent)
        .where(MemoryCurrent.workspace_id == ctx.workspace_id)
        .order_by(MemoryCurrent.created_at.desc(), MemoryCurrent.id.desc())
        # One extra row is the cheapest way to know whether a next page exists
        # without a second COUNT query.
        .limit(checked_limit + 1)
    )
    if after is not None:
        # A row-value comparison, which Postgres evaluates as the lexicographic
        # "(created_at, id) strictly before the cursor" that keyset pagination
        # means — and which an index on (created_at, id) can serve directly.
        stmt = stmt.where(tuple_(MemoryCurrent.created_at, MemoryCurrent.id) < after)

    rows = list(session.scalars(stmt))
    has_more = len(rows) > checked_limit
    page = rows[:checked_limit]
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    return MemoryPage(items=[MemoryRecord.from_model(row) for row in page], next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
#
# The queries below read ``memories_current`` directly rather than going through
# ``Repo``. ``Repo.current_memories()`` returns the whole view in creation order
# and has no substring filter and no keyset predicate, and ``purse/db`` is owned
# by the data layer — adding methods there is a C1 change, not a C3 one. These
# are typed SQLAlchemy selects over the mapped view, not raw SQL, and every one
# of them carries the ``workspace_id`` predicate explicitly, which is the same
# invariant ``Repo`` enforces structurally. If a third caller needs them, they
# should be promoted onto ``Repo``.


def _get_current(
    session: Session, workspace_id: uuid.UUID, memory_id: uuid.UUID
) -> MemoryCurrent | None:
    stmt = select(MemoryCurrent).where(
        MemoryCurrent.workspace_id == workspace_id, MemoryCurrent.id == memory_id
    )
    return session.scalars(stmt).one_or_none()


def _get_current_many(
    session: Session, workspace_id: uuid.UUID, memory_ids: Sequence[uuid.UUID]
) -> list[MemoryCurrent]:
    if not memory_ids:
        return []
    stmt = select(MemoryCurrent).where(
        MemoryCurrent.workspace_id == workspace_id, MemoryCurrent.id.in_(memory_ids)
    )
    return list(session.scalars(stmt))


def _like_escape(value: str) -> str:
    """Neutralise ``LIKE`` wildcards so a query means what it says.

    Without this, searching for ``100%`` matches every memory, and searching for
    ``a_b`` matches ``axb``. The backslash must be escaped first or it would
    escape the escapes.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fallback_search(
    session: Session, workspace_id: uuid.UUID, query: str, limit: int
) -> list[SearchHit]:
    pattern = f"%{_like_escape(query.strip())}%"
    stmt = (
        select(MemoryCurrent)
        .where(
            MemoryCurrent.workspace_id == workspace_id,
            MemoryCurrent.content.ilike(pattern, escape="\\"),
        )
        .order_by(MemoryCurrent.created_at.desc(), MemoryCurrent.id.desc())
        .limit(limit)
    )
    return [SearchHit.from_record(MemoryRecord.from_model(row)) for row in session.scalars(stmt)]


def _engine_search(
    engine: MemoryEngine, workspace_id: uuid.UUID, query: str, limit: int
) -> list[EngineHit]:
    try:
        return list(engine.search(workspace_id, query, limit))
    except Exception:
        logger.warning(
            "memory engine search failed for workspace %s; falling back to canonical search",
            workspace_id,
            exc_info=True,
        )
        return []


def _ingest(engine: MemoryEngine, record: MemoryRecord, workspace_id: uuid.UUID) -> None:
    """Best-effort indexing. The canonical write has already happened.

    This is the rule from :mod:`purse.memory.engine` made concrete: an engine
    that raises must not turn a durable write into an error response. The
    warning (with traceback) is the operator's signal that the index is drifting
    and wants a rebuild (C3.6).
    """
    try:
        engine.ingest(record, workspace_id=workspace_id)
    except Exception:
        logger.warning(
            "memory engine ingest failed for %s in workspace %s; "
            "canonical write is committed, index is stale",
            record.id,
            workspace_id,
            exc_info=True,
        )


def _audit(repo: Repo, ctx: WriteContext, *, action: str, target_id: uuid.UUID) -> None:
    repo.record_audit(
        connection_id=ctx.connection_id,
        action=action,
        target_type=_AUDIT_TARGET,
        target_id=str(target_id),
        agent_id=ctx.agent_id,
    )
