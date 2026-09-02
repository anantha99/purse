"""C1.3 — memories: append-only, enforced by Postgres triggers.

Revision ID: c1_3_memories
Revises: c1_2_connections

The immutability contract this migration installs
-------------------------------------------------
``memories`` is the canonical log (PRD §8.2). Its guarantee is not "the
application does not run UPDATE statements" — it is "Postgres refuses". A
mistaken migration, a psql session, an ORM flush, and a future contributor all
hit the same wall.

Allowed mutation surface (everything else raises):

* ``tombstone`` may be flipped ``false -> true``. It may never be cleared.
* ``embedding`` may be written freely. It is *derived* data — a cache of
  ``content`` under whatever embedding model is configured — and C3.6 requires
  the index to be droppable and rebuildable in place. Nothing a user said is
  stored there.

Immutable: ``id``, ``workspace_id``, ``content``, ``kind``, ``supersedes``,
``connection_id``, ``agent_id``, ``initiated_by``, ``created_at``. That is the
content and the whole provenance chain.

``DELETE`` and ``TRUNCATE`` are rejected outright. A genuine hard erase is a
compliance path only (PRD §11): it requires an operator to disable the trigger
explicitly, which is deliberately manual, obvious in the audit trail, and not
something application code can reach.

Supersession is an INSERT: a new row whose ``supersedes`` points at the old id.
The composite foreign key ``(supersedes, workspace_id) -> (id, workspace_id)``
makes cross-workspace supersession structurally impossible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "c1_3_memories"
down_revision: str | None = "c1_2_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMORY_KINDS = ("fact", "preference", "decision")
MEMORY_INITIATORS = ("user", "agent")

# Width of the pgvector column, hard-coded on purpose.
#
# purse.db.models.EMBEDDING_DIM is the source of truth for the *application*;
# this constant records what THIS migration created. They must agree, and
# tests/db/test_schema.py fails if they ever stop agreeing. Importing the
# constant here instead would mean editing it silently changes history: fresh
# databases would get the new width while migrated ones kept the old, with no
# error anywhere. Changing the dimension is a new migration (ALTER COLUMN +
# rebuild the vector index), not an edit to this file.
EMBEDDING_DIM_AT_C1_3 = 1536

_REJECT_UPDATE_FN = """
CREATE OR REPLACE FUNCTION purse_memories_reject_update() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    changed text[];
BEGIN
    -- Whole-row comparison rather than a hand-listed column set: a column
    -- added by a later migration is immutable by default, which is the safe
    -- direction to fail. `embedding` and `tombstone` are stripped from both
    -- sides because they are the entire permitted mutation surface.
    SELECT coalesce(array_agg(key ORDER BY key), ARRAY[]::text[])
      INTO changed
      FROM (
          SELECT key, value FROM jsonb_each(to_jsonb(NEW) - 'embedding' - 'tombstone')
          EXCEPT
          SELECT key, value FROM jsonb_each(to_jsonb(OLD) - 'embedding' - 'tombstone')
      ) AS diff;

    IF cardinality(changed) > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = 'memories is append-only: cannot UPDATE column(s) '
                      || array_to_string(changed, ', '),
            DETAIL  = 'memory id ' || OLD.id::text,
            HINT    = 'to change a memory, INSERT a new row with supersedes set to the '
                      || 'old id; to remove one, UPDATE ... SET tombstone = true';
    END IF;

    IF OLD.tombstone AND NOT NEW.tombstone THEN
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = 'memories.tombstone may only be set, never cleared',
            DETAIL  = 'memory id ' || OLD.id::text,
            HINT    = 'a tombstoned memory stays tombstoned; write a new memory instead';
    END IF;

    RETURN NEW;
END;
$fn$;
"""

_REJECT_DELETE_FN = """
CREATE OR REPLACE FUNCTION purse_memories_reject_delete() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'restrict_violation',
        MESSAGE = 'memories is append-only: rows may not be DELETEd',
        DETAIL  = 'memory id ' || OLD.id::text,
        HINT    = 'UPDATE ... SET tombstone = true. A hard erase is a compliance path: '
                  || 'ALTER TABLE memories DISABLE TRIGGER purse_memories_no_delete, '
                  || 'delete, then re-enable.';
    RETURN NULL;
END;
$fn$;
"""

_REJECT_TRUNCATE_FN = """
CREATE OR REPLACE FUNCTION purse_memories_reject_truncate() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'restrict_violation',
        MESSAGE = 'memories is append-only: the table may not be TRUNCATEd',
        HINT    = 'DROP the database if you want a clean slate; the canonical log '
                  || 'does not support partial erasure by convenience.';
    RETURN NULL;
END;
$fn$;
"""


def upgrade() -> None:
    kind = postgresql.ENUM(*MEMORY_KINDS, name="memory_kind", create_type=False)
    initiator = postgresql.ENUM(*MEMORY_INITIATORS, name="memory_initiator", create_type=False)
    kind.create(op.get_bind(), checkfirst=True)
    initiator.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "memories",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", kind, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM_AT_C1_3), nullable=True),
        sa.Column("supersedes", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("tombstone", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("connection_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("initiated_by", initiator, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memories"),
        # Target for the composite self-reference below.
        sa.UniqueConstraint("id", "workspace_id", name="uq_memories_id_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_memories_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        # A memory may only supersede a memory in the same workspace. Enforced
        # by the shape of the key, not by a check the application might forget.
        sa.ForeignKeyConstraint(
            ["supersedes", "workspace_id"],
            ["memories.id", "memories.workspace_id"],
            name="fk_memories_supersedes_memories",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_memories_connection_id_connections",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("supersedes IS NULL OR supersedes <> id", name="no_self_supersede"),
    )

    op.create_index(
        "ix_memories_workspace_id_created_at", "memories", ["workspace_id", "created_at"]
    )
    op.create_index("ix_memories_supersedes", "memories", ["supersedes"])

    # HNSW over ivfflat: no training step, no "build the index after you have
    # data" footgun, better recall/latency at MVP volumes. Cosine distance
    # because text embedding models are trained for it and the vectors that
    # will land here are normalized. A different metric is a new index, not a
    # schema change.
    op.execute(
        "CREATE INDEX ix_memories_embedding_hnsw ON memories "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(_REJECT_UPDATE_FN)
    op.execute(_REJECT_DELETE_FN)
    op.execute(_REJECT_TRUNCATE_FN)
    op.execute(
        "CREATE TRIGGER purse_memories_no_update BEFORE UPDATE ON memories "
        "FOR EACH ROW EXECUTE FUNCTION purse_memories_reject_update()"
    )
    op.execute(
        "CREATE TRIGGER purse_memories_no_delete BEFORE DELETE ON memories "
        "FOR EACH ROW EXECUTE FUNCTION purse_memories_reject_delete()"
    )
    op.execute(
        "CREATE TRIGGER purse_memories_no_truncate BEFORE TRUNCATE ON memories "
        "FOR EACH STATEMENT EXECUTE FUNCTION purse_memories_reject_truncate()"
    )

    op.execute(
        "COMMENT ON TABLE memories IS "
        "'Append-only canonical memory log. Only tombstone (false->true) and "
        "embedding may be updated; DELETE and TRUNCATE are rejected by trigger. "
        "Supersession is an INSERT with supersedes set.'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS purse_memories_no_truncate ON memories")
    op.execute("DROP TRIGGER IF EXISTS purse_memories_no_delete ON memories")
    op.execute("DROP TRIGGER IF EXISTS purse_memories_no_update ON memories")
    op.execute("DROP FUNCTION IF EXISTS purse_memories_reject_truncate()")
    op.execute("DROP FUNCTION IF EXISTS purse_memories_reject_delete()")
    op.execute("DROP FUNCTION IF EXISTS purse_memories_reject_update()")
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.drop_index("ix_memories_supersedes", table_name="memories")
    op.drop_index("ix_memories_workspace_id_created_at", table_name="memories")
    op.drop_table("memories")
    postgresql.ENUM(name="memory_initiator").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="memory_kind").drop(op.get_bind(), checkfirst=True)
