"""C1.5 — skills and skill_heads.

Revision ID: c1_5_skills
Revises: c1_4_memories_current

``skills`` rows are immutable versions; ``skill_heads`` is the mutable pointer
from ``(workspace, name)`` to the version ``get_skill(name)`` resolves to. The
head FK is ``ON DELETE RESTRICT`` so a version can never be removed while it is
the one clients are being served.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1_5_skills"
down_revision: str | None = "c1_4_memories_current"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # Semver as text. Ordering is done by the application (C5.3), which
        # knows that 1.10.0 > 1.9.0; a text collation does not.
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "frontmatter",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        # sha256 hex of content — content-addressed versions (PRD §8.3).
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_skills_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skills"),
        sa.UniqueConstraint(
            "workspace_id", "name", "version", name="uq_skills_workspace_id_name_version"
        ),
    )
    op.create_index("ix_skills_workspace_id_name", "skills", ["workspace_id", "name"])

    op.create_table(
        "skill_heads",
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("skill_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_skill_heads_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name="fk_skill_heads_skill_id_skills", ondelete="RESTRICT"
        ),
        # The composite primary key is the uniqueness guarantee: one head per
        # (workspace, name).
        sa.PrimaryKeyConstraint("workspace_id", "name", name="pk_skill_heads"),
    )


def downgrade() -> None:
    op.drop_table("skill_heads")
    op.drop_index("ix_skills_workspace_id_name", table_name="skills")
    op.drop_table("skills")
