"""C1.8 — monotonic ordering for audit_log.

Revision ID: c1_8_audit_seq
Revises: c1_7_audit_log

``created_at`` defaults to ``now()``, which in Postgres is *transaction*
time — every audit entry written inside one transaction carries an identical
timestamp, and the previous ``ORDER BY created_at DESC, id DESC`` then
tiebreaks on a random uuid: "newest first" degraded to a coin flip. The audit
view is a product surface ("last 100 writes"), so insertion order must be a
guarantee, not a probability.

``seq`` is an always-generated identity: strictly monotonic per insert,
backfilled by Postgres for existing rows at ALTER time in id-scan order.
Ordering queries use it exclusively; ``created_at`` remains for display.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c1_8_audit_seq"
down_revision: str | None = "c1_7_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_log ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX ix_audit_log_workspace_id_seq ON audit_log (workspace_id, seq DESC)"
    )
    op.execute("DROP INDEX IF EXISTS ix_audit_log_workspace_id_created_at")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX ix_audit_log_workspace_id_created_at "
        "ON audit_log (workspace_id, created_at DESC)"
    )
    op.execute("DROP INDEX IF EXISTS ix_audit_log_workspace_id_seq")
    op.execute("ALTER TABLE audit_log DROP COLUMN seq")
