"""Index rebuild command (C3.6): drop the derived index, replay the canonical log.

PRD §3.1-2 / §8.2 make a promise about the derived index: it is *derived,
droppable, rebuildable*. Nothing of value lives only there — drop it and replay
the canonical store and you get an equivalent index back. This module is the
executable proof of that promise, and the operator's tool when the index has
drifted (an ``ingest`` that failed while the canonical write succeeded logs a
warning; this is how the warning is resolved).

``rebuild_workspace`` is the unit: ``drop`` the workspace's index, then replay its
**current view** (not the whole append-only log — superseded and tombstoned rows
are not current, so they do not come back) through ``ingest``. Because the engine
indexes verbatim with ``infer=False``, one canonical row in is one vector row out:
the rebuild is deterministic and idempotent.

Run it as a command::

    python -m purse.memory.rebuild                 # every workspace
    python -m purse.memory.rebuild <workspace-uuid>  # just one

It reads ``DATABASE_URL`` and the ``PURSE_EMBEDDING_*`` vars (same as the app). If
no embedding key is configured the engine is a ``NullEngine`` and the rebuild is a
no-op that says so, rather than a silent success that indexed nothing.
"""

from __future__ import annotations

import logging
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from purse.db.models import Workspace
from purse.db.repo import Repo
from purse.db.session import create_db_engine, session_scope
from purse.memory.engine import MemoryEngine, NullEngine
from purse.memory.mem0_engine import build_memory_engine_from_env
from purse.memory.records import MemoryRecord

logger = logging.getLogger(__name__)

__all__ = ["rebuild_all", "rebuild_workspace"]


def rebuild_workspace(session: Session, engine: MemoryEngine, workspace_id: uuid.UUID) -> int:
    """Drop and replay one workspace's index from its current view. Returns rows replayed.

    ``Repo.open`` raises if the workspace does not exist, which is the right
    failure for a command handed a bad id.
    """
    repo = Repo.open(session, workspace_id)
    engine.drop(workspace_id)
    replayed = 0
    for row in repo.current_memories():
        engine.ingest(MemoryRecord.from_model(row), workspace_id=workspace_id)
        replayed += 1
    logger.info("rebuilt workspace %s: replayed %d current memories", workspace_id, replayed)
    return replayed


def rebuild_all(session: Session, engine: MemoryEngine) -> dict[uuid.UUID, int]:
    """Rebuild every workspace's index. Returns a per-workspace replay count."""
    workspace_ids = list(session.scalars(select(Workspace.id).order_by(Workspace.created_at)))
    return {ws_id: rebuild_workspace(session, engine, ws_id) for ws_id in workspace_ids}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Rebuild all workspaces, or the one named on the argv."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:] if argv is None else argv

    if len(args) > 1:
        print("usage: python -m purse.memory.rebuild [workspace-uuid]", file=sys.stderr)
        return 2

    target: uuid.UUID | None = None
    if args:
        try:
            target = uuid.UUID(args[0])
        except ValueError:
            print(f"not a workspace uuid: {args[0]!r}", file=sys.stderr)
            return 2

    engine = build_memory_engine_from_env()
    if isinstance(engine, NullEngine):
        print(
            "No embedding key configured (PURSE_EMBEDDING_API_KEY); the index engine is "
            "NullEngine and there is nothing to rebuild. Set an embedding provider to enable "
            "semantic recall.",
            file=sys.stderr,
        )
        return 0

    db_engine = create_db_engine()
    try:
        with session_scope(db_engine) as session:
            if target is not None:
                replayed = rebuild_workspace(session, engine, target)
                print(f"rebuilt {target}: {replayed} memories")
            else:
                counts = rebuild_all(session, engine)
                total = sum(counts.values())
                print(f"rebuilt {len(counts)} workspace(s): {total} memories")
    finally:
        db_engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess/command
    raise SystemExit(main())
