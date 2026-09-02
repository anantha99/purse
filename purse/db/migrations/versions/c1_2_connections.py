"""C1.2 — connections and oauth_clients.

Revision ID: c1_2_connections
Revises: c1_1_baseline
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1_2_connections"
down_revision: str | None = "c1_1_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum labels are written out here rather than imported from purse.db.models on
# purpose: a migration must keep producing the schema it produced on the day it
# was written. Adding a mode later is a new migration with ALTER TYPE ... ADD
# VALUE, not an edit to this file.
AUTH_MODES = ("oauth_dcr", "oauth_cimd", "oauth_static", "pat")
OAUTH_CLIENT_KINDS = ("dcr", "cimd", "static")


def upgrade() -> None:
    auth_mode = postgresql.ENUM(*AUTH_MODES, name="auth_mode", create_type=False)
    client_kind = postgresql.ENUM(*OAUTH_CLIENT_KINDS, name="oauth_client_kind", create_type=False)
    auth_mode.create(op.get_bind(), checkfirst=True)
    client_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "connections",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("auth_mode", auth_mode, nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("writes_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # SHA-256 hex of the bearer token, never the token (C2.1).
        sa.Column("token_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_connections_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connections"),
    )
    op.create_index("ix_connections_workspace_id", "connections", ["workspace_id"])
    # Token lookup is on the hash, and two connections must never share one.
    # Partial, because NULL token_hash (OAuth connections) is the common case.
    op.create_index(
        "uq_connections_token_hash",
        "connections",
        ["token_hash"],
        unique=True,
        postgresql_where=sa.text("token_hash IS NOT NULL"),
    )

    op.create_table(
        "oauth_clients",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", client_kind, nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_clients"),
        sa.UniqueConstraint("client_id", name="uq_oauth_clients_client_id"),
    )


def downgrade() -> None:
    op.drop_table("oauth_clients")
    op.drop_index("uq_connections_token_hash", table_name="connections")
    op.drop_index("ix_connections_workspace_id", table_name="connections")
    op.drop_table("connections")
    postgresql.ENUM(name="oauth_client_kind").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="auth_mode").drop(op.get_bind(), checkfirst=True)
