"""SQLAlchemy 2.0 typed models for the Purse vault (PRD §11).

Design notes that are not obvious from the column list:

* **``memories`` is append-only, enforced in Postgres, not in Python.**
  Triggers reject every ``UPDATE`` that changes anything other than flipping
  ``tombstone`` from false to true, and reject every ``DELETE``. Application
  code cannot opt out, and neither can a mistaken migration, a psql session, or
  a future contributor who "just needs to fix one row". See
  ``purse/db/migrations/versions/*_memories.py`` for the trigger bodies and
  ``docs/export-schema.md`` for what that guarantees an export.

* **Supersession is an insert, never an update.** ``update_memory`` writes a new
  row whose ``supersedes`` points at the old one. The log is the history.

* **``memories_current`` is a plain view**, mapped here read-only as
  :class:`MemoryCurrent`. See the C1.4 migration for why it is not
  materialized.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "EMBEDDING_DIM",
    "Api",
    "AuditLogEntry",
    "AuthMode",
    "Base",
    "Connection",
    "InitiatedBy",
    "Memory",
    "MemoryCurrent",
    "MemoryKind",
    "OAuthClient",
    "OAuthClientKind",
    "Skill",
    "SkillHead",
    "User",
    "Workspace",
]

# ---------------------------------------------------------------------------
# Embedding dimension — the single source of truth
# ---------------------------------------------------------------------------

#: Dimension of the ``memories.embedding`` pgvector column.
#:
#: 1536 is a placeholder chosen because it is the width of the most common
#: general-purpose embedding models (OpenAI ``text-embedding-3-small``, and the
#: default many local models are configured to match). **The actual embedding
#: model is a C3 decision** — Mem0's ``embedding_model_dims`` must be set to
#: this same number, and getting it wrong is a documented silent-data-loss bug
#: in Mem0, which is why the value lives in exactly one place.
#:
#: Changing this constant is NOT sufficient to change the deployed schema: the
#: baseline migration hard-codes the width it created (an old migration must
#: keep producing the schema it always produced, or upgraded databases and
#: fresh databases silently diverge). A change here requires a new migration
#: that ``ALTER``s the column and rebuilds the vector index.
#: ``tests/db/test_schema.py::test_embedding_dimension_matches_the_constant``
#: fails if the two ever drift apart.
EMBEDDING_DIM = 1536

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

# Deterministic constraint/index names, so migrations, models, and anything that
# has to drop a constraint by name all agree without guessing.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every Purse table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Shared column definitions ---------------------------------------------------


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# ---------------------------------------------------------------------------
# Enums (native Postgres enum types)
# ---------------------------------------------------------------------------


class AuthMode(enum.StrEnum):
    """How a connection authenticated (PRD §8.5). One per supported mode."""

    OAUTH_DCR = "oauth_dcr"
    OAUTH_CIMD = "oauth_cimd"
    OAUTH_STATIC = "oauth_static"
    PAT = "pat"


class OAuthClientKind(enum.StrEnum):
    """Provenance of a registered OAuth client record."""

    DCR = "dcr"
    CIMD = "cimd"
    STATIC = "static"


class MemoryKind(enum.StrEnum):
    """Durable-fact taxonomy (PRD §8.2). ``profile`` is explicitly post-MVP."""

    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"


class InitiatedBy(enum.StrEnum):
    """Who asked for the write.

    Recorded as a *claim* made by the calling agent (PRD §10 / C4.7): the
    trusted provenance is ``connection_id``, not this field.
    """

    USER = "user"
    AGENT = "agent"


def _pg_enum(python_enum: type[enum.StrEnum], name: str) -> PgEnum:
    """A native Postgres enum whose labels are the enum *values*, not names."""
    return PgEnum(
        python_enum,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        create_type=False,
    )


AUTH_MODE_ENUM = _pg_enum(AuthMode, "auth_mode")
OAUTH_CLIENT_KIND_ENUM = _pg_enum(OAuthClientKind, "oauth_client_kind")
MEMORY_KIND_ENUM = _pg_enum(MemoryKind, "memory_kind")
INITIATED_BY_ENUM = _pg_enum(InitiatedBy, "memory_initiator")


# ---------------------------------------------------------------------------
# Identity (C1.1)
# ---------------------------------------------------------------------------


class User(Base):
    """One human. One vault (PRD §8.1)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Auth material is a jsonb envelope so the shape can evolve with C2 without
    # a migration per auth mode. Password hashes / provider subject IDs live
    # here; raw secrets never do.
    auth: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()


class Workspace(Base):
    """Hard isolation boundary. Every query is scoped to exactly one (PRD §8.1)."""

    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_workspaces_user_id_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_workspaces_user_id_users"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()


# ---------------------------------------------------------------------------
# Connections and OAuth clients (C1.2)
# ---------------------------------------------------------------------------


class Connection(Base):
    """A client that has been granted access to a workspace.

    ``connection_id`` is the trusted provenance recorded on every write.
    """

    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "workspaces.id", ondelete="CASCADE", name="fk_connections_workspace_id_workspaces"
        ),
        nullable=False,
        index=True,
    )
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    auth_mode: Mapped[AuthMode] = mapped_column(AUTH_MODE_ENUM, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    writes_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # SHA-256 of the bearer token. Never the token itself (C2.1).
    token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthClient(Base):
    """A registered OAuth client: DCR churn, CIMD documents, static registrations."""

    __tablename__ = "oauth_clients"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[OAuthClientKind] = mapped_column(OAUTH_CLIENT_KIND_ENUM, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Attribute is renamed because `metadata` is reserved on a declarative class.
    client_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()


# ---------------------------------------------------------------------------
# Memories (C1.3) — append-only
# ---------------------------------------------------------------------------


class _MemoryColumns:
    """Columns shared by the ``memories`` table and the ``memories_current`` view.

    A declarative mixin, so the view mapping can never drift from the table it
    is derived from.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[MemoryKind] = mapped_column(MEMORY_KIND_ENUM, nullable=False)
    # Nullable: the canonical write is synchronous, embedding is computed by the
    # derived-index path (C3) and backfilled. A memory with no embedding is a
    # real memory that simply is not searchable yet.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    supersedes: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tombstone: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    initiated_by: Mapped[InitiatedBy] = mapped_column(INITIATED_BY_ENUM, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Memory(_MemoryColumns, Base):
    """The canonical memory log. Insert-only; see the module docstring.

    Foreign keys and indexes are declared here rather than on the mixin: the
    view mapping shares the columns but must not claim the constraints.
    """

    __tablename__ = "memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_memories_workspace_id_workspaces",
        ),
        # Composite: a memory can only supersede a memory in the same workspace.
        UniqueConstraint("id", "workspace_id", name="uq_memories_id_workspace_id"),
        ForeignKeyConstraint(
            ["supersedes", "workspace_id"],
            ["memories.id", "memories.workspace_id"],
            ondelete="RESTRICT",
            name="fk_memories_supersedes_memories",
        ),
        ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            ondelete="RESTRICT",
            name="fk_memories_connection_id_connections",
        ),
        CheckConstraint("supersedes IS NULL OR supersedes <> id", name="no_self_supersede"),
        Index("ix_memories_workspace_id_created_at", "workspace_id", "created_at"),
        Index("ix_memories_supersedes", "supersedes"),
    )


class MemoryCurrent(_MemoryColumns, Base):
    """Read-only mapping of the ``memories_current`` view (C1.4).

    Not a table: never insert into it, never migrate it with ``create_all``.
    """

    __tablename__ = "memories_current"
    # `info` marks this mapping as a view so Alembic autogenerate leaves it
    # alone (see purse/db/migrations/env.py::include_object).
    __table_args__ = ({"info": {"purse_view": True}},)


# ---------------------------------------------------------------------------
# Skills (C1.5)
# ---------------------------------------------------------------------------


class Skill(Base):
    """One immutable version of a skill. Content-addressed by ``content_hash``."""

    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "name", "version", name="uq_skills_workspace_id_name_version"
        ),
        Index("ix_skills_workspace_id_name", "workspace_id", "name"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE", name="fk_skills_workspace_id_workspaces"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    frontmatter: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()


class SkillHead(Base):
    """``(workspace, name)`` → the version that ``get_skill(name)`` resolves to.

    Mutable by design: this is a pointer, the history lives in ``skills``.
    """

    __tablename__ = "skill_heads"
    __table_args__ = (PrimaryKeyConstraint("workspace_id", "name", name="pk_skill_heads"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "workspaces.id", ondelete="CASCADE", name="fk_skill_heads_workspace_id_workspaces"
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("skills.id", ondelete="RESTRICT", name="fk_skill_heads_skill_id_skills"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# APIs (C1.6)
# ---------------------------------------------------------------------------


class Api(Base):
    """A stored third-party API credential (C6 owns the crypto; this is storage).

    ``key_ciphertext`` and ``dek_wrapped`` are the only two columns in the whole
    schema that hold key material. They are never exported (C1.9), never
    logged, and never returned by an MCP tool.
    """

    __tablename__ = "apis"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_apis_workspace_id_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE", name="fk_apis_workspace_id_workspaces"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_style: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_hosts: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dek_wrapped: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()
    rotated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Audit log (C1.7)
# ---------------------------------------------------------------------------


class AuditLogEntry(Base):
    """Names and IDs only. Never values, never key material (PRD §11, §13)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index(
            "ix_audit_log_workspace_id_created_at",
            "workspace_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "workspaces.id", ondelete="CASCADE", name="fk_audit_log_workspace_id_workspaces"
        ),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "connections.id", ondelete="RESTRICT", name="fk_audit_log_connection_id_connections"
        ),
        nullable=False,
    )
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()
