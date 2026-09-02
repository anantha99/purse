"""C1.4 — the memories_current view.

Revision ID: c1_4_memories_current
Revises: c1_3_memories

Two decisions are recorded here.

1. Plain VIEW, not MATERIALIZED VIEW (deviation from PRD §11)
------------------------------------------------------------
The PRD says "materialized". A materialized view needs a refresh strategy, and
every refresh strategy at MVP write volumes buys a class of staleness bug that
is worse than the cost it avoids: a memory saved in Cursor is not visible in
Claude Code until a refresh lands, which is precisely the cross-tool recall
demo the product is judged on (PRD §14). A plain view is always correct.

The query is two index-backed scans (``ix_memories_workspace_id_created_at``
and ``ix_memories_supersedes``) over a table that holds, per workspace, a
human's durable facts — thousands of rows, not millions. Materialization is a
later optimization, to be taken when write volume justifies it and with a
refresh strategy chosen deliberately (``REFRESH ... CONCURRENTLY`` on a unique
index, triggered off the write path).

2. "Superseded" means *any* successor exists, not *a live successor* exists
--------------------------------------------------------------------------
Supersession is a permanent historical fact: once B is written saying "this
replaces A", A is spent. Whether B is itself later superseded or tombstoned
changes nothing about A.

The alternative reading — "superseded by a row that is still current" — has a
resurrection bug. Given A <- B <- C, tombstoning C would make B's successor
"not live", so B would pop back into the current set, and a user who deleted
their latest note would silently get an older version of it back. Chains die
at their head:

    A <- B <- C, C tombstoned  =>  memories_current is empty for that chain.

``memories`` rows are never deleted, so "a successor row exists" is equivalent
to "a successor was ever written", which is exactly the intended semantics.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c1_4_memories_current"
down_revision: str | None = "c1_3_memories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATE_VIEW = """
CREATE VIEW memories_current AS
SELECT m.id,
       m.workspace_id,
       m.content,
       m.kind,
       m.embedding,
       m.supersedes,
       m.tombstone,
       m.connection_id,
       m.agent_id,
       m.initiated_by,
       m.created_at
  FROM memories AS m
 WHERE m.tombstone IS FALSE
   AND NOT EXISTS (
       SELECT 1
         FROM memories AS successor
        WHERE successor.supersedes = m.id
   );
"""


def upgrade() -> None:
    op.execute(_CREATE_VIEW)
    op.execute(
        "COMMENT ON VIEW memories_current IS "
        "'Live memories: not tombstoned and never superseded. A plain view, not "
        "materialized, so cross-tool recall is never stale; see migration "
        "c1_4_memories_current for the reasoning and the chain semantics.'"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS memories_current")
