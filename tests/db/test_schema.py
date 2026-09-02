"""The migrated schema is the schema the code believes in (C1.1 through C1.7)."""

from __future__ import annotations

import enum

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from purse.db.models import EMBEDDING_DIM, AuthMode, InitiatedBy, MemoryKind, OAuthClientKind

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "users",
    "workspaces",
    "connections",
    "oauth_clients",
    "memories",
    "skills",
    "skill_heads",
    "apis",
    "audit_log",
}


def _scalars(session: Session, sql: str, **params: object) -> list[object]:
    return list(session.execute(text(sql), params).scalars())


def test_pgvector_extension_is_installed(session: Session) -> None:
    assert _scalars(session, "SELECT extname FROM pg_extension WHERE extname = 'vector'") == [
        "vector"
    ]


def test_every_table_exists(session: Session) -> None:
    found = set(
        _scalars(
            session,
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()",
        )
    )
    assert found >= EXPECTED_TABLES


def test_memories_current_is_a_plain_view_not_materialized(session: Session) -> None:
    """C1.4: correctness over refresh strategy at MVP scale."""
    assert _scalars(
        session, "SELECT viewname FROM pg_views WHERE viewname = 'memories_current'"
    ) == ["memories_current"]
    assert (
        _scalars(
            session,
            "SELECT matviewname FROM pg_matviews WHERE matviewname = 'memories_current'",
        )
        == []
    )


def test_embedding_dimension_matches_the_constant(session: Session) -> None:
    """The deployed column width and ``EMBEDDING_DIM`` must never drift apart.

    The C1.3 migration hard-codes the width it created, on purpose (an old
    migration must keep producing the schema it always produced). This test is
    what makes that safe: change the constant without writing the ALTER
    migration and CI fails here.
    """
    declared = _scalars(
        session,
        """
        SELECT format_type(a.atttypid, a.atttypmod)
          FROM pg_attribute a
         WHERE a.attrelid = 'memories'::regclass
           AND a.attname = 'embedding'
        """,
    )
    assert declared == [f"vector({EMBEDDING_DIM})"]


def test_append_only_triggers_are_installed(session: Session) -> None:
    triggers = set(
        _scalars(
            session,
            """
            SELECT tgname FROM pg_trigger
             WHERE tgrelid = 'memories'::regclass AND NOT tgisinternal
            """,
        )
    )
    assert triggers == {
        "purse_memories_no_update",
        "purse_memories_no_delete",
        "purse_memories_no_truncate",
    }


def test_memory_indexes_exist_including_an_hnsw_vector_index(session: Session) -> None:
    definitions: dict[str, str] = {}
    for name, definition in session.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'memories'")
    ).all():
        definitions[name] = definition
    assert "ix_memories_workspace_id_created_at" in definitions
    assert "ix_memories_supersedes" in definitions
    hnsw = definitions["ix_memories_embedding_hnsw"]
    assert "USING hnsw" in hnsw
    assert "vector_cosine_ops" in hnsw


def test_audit_log_index_is_descending_on_seq(session: Session) -> None:
    definition = session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_audit_log_workspace_id_seq'")
    ).scalar_one()
    assert "seq DESC" in definition
    assert "UNIQUE" in definition


@pytest.mark.parametrize(
    ("type_name", "python_enum"),
    [
        ("auth_mode", AuthMode),
        ("oauth_client_kind", OAuthClientKind),
        ("memory_kind", MemoryKind),
        ("memory_initiator", InitiatedBy),
    ],
)
def test_postgres_enum_labels_match_the_python_enums(
    session: Session, type_name: str, python_enum: type[enum.StrEnum]
) -> None:
    labels = _scalars(
        session,
        """
        SELECT e.enumlabel
          FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
         WHERE t.typname = :type_name
         ORDER BY e.enumsortorder
        """,
        type_name=type_name,
    )
    assert labels == [member.value for member in list(python_enum)]
