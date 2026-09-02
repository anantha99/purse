"""C1.7 — audit_log.

Revision ID: c1_7_audit_log
Revises: c1_6_apis

Names and IDs only (PRD §11, §13). ``target_id`` is text rather than uuid
because targets are heterogeneous — a memory uuid, an API name, a connection
uuid — and the audit trail must never fail to record something because it did
not fit a column type.

``connection_id`` is NOT NULL: every audited action has a connection behind it,
and "which client did this" is the whole point of the table. Background work
that has no client of its own gets a connection row of its own.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1_7_audit_log"
down_revision: str | None = "c1_6_apis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("connection_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_audit_log_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_audit_log_connection_id_connections",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    # DESC matches the only read pattern: "the last N things that happened in
    # this workspace" (C7.7).
    op.execute(
        "CREATE INDEX ix_audit_log_workspace_id_created_at "
        "ON audit_log (workspace_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_log_workspace_id_created_at")
    op.drop_table("audit_log")
