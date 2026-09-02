"""C1.6 — apis.

Revision ID: c1_6_apis
Revises: c1_5_skills

``key_ciphertext`` and ``dek_wrapped`` are the only columns in the schema that
hold key material (envelope encryption, C6.1). They are excluded from the vault
export by construction (see purse/db/export.py) and must never appear in an MCP
response, a log line, or an audit row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1_6_apis"
down_revision: str | None = "c1_5_skills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apis",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        # Bearer | header for MVP (C6.7). Text rather than an enum so adding an
        # auth style is a code change, not a migration.
        sa.Column("auth_style", sa.Text(), nullable=False),
        sa.Column(
            "allowed_hosts",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("key_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("dek_wrapped", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_apis_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_apis"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_apis_workspace_id_name"),
    )
    op.create_index("ix_apis_workspace_id", "apis", ["workspace_id"])
    op.execute(
        "COMMENT ON COLUMN apis.key_ciphertext IS "
        "'Envelope-encrypted API key. Never exported, logged, or returned.'"
    )
    op.execute(
        "COMMENT ON COLUMN apis.dek_wrapped IS "
        "'Per-key DEK wrapped by KMS or the local master keyfile. Never exported.'"
    )


def downgrade() -> None:
    op.drop_index("ix_apis_workspace_id", table_name="apis")
    op.drop_table("apis")
