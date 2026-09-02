"""C1 unit tests that need no database.

These run everywhere, including in the lint-and-typecheck job, so the parts of
the data layer that are pure logic — URL handling, the export field allowlist,
the model/migration agreement — fail fast rather than waiting for a Postgres.
"""

from __future__ import annotations

import pytest

from purse.db import export as export_module
from purse.db.config import (
    DatabaseUrlError,
    database_url,
    normalize_database_url,
)
from purse.db.migrate import MIGRATIONS_DIR, alembic_config
from purse.db.models import EMBEDDING_DIM, Api, Base, Connection, Memory, MemoryCurrent
from purse.db.repo import Repo, content_hash
from tests.conftest import database_required

# -- configuration ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://purse:pw@localhost:5432/purse",
        "postgres://purse:pw@localhost:5432/purse",
        "postgresql+psycopg://purse:pw@localhost:5432/purse",
    ],
)
def test_every_postgres_spelling_normalizes_to_psycopg(raw: str) -> None:
    assert normalize_database_url(raw).startswith("postgresql+psycopg://")


def test_normalization_preserves_the_rest_of_the_url() -> None:
    normalized = normalize_database_url(
        "postgresql://purse:pw@db.example:6543/vault?sslmode=require"
    )
    assert "@db.example:6543/vault" in normalized
    assert "sslmode=require" in normalized


def test_a_non_postgres_url_is_rejected() -> None:
    with pytest.raises(DatabaseUrlError):
        normalize_database_url("sqlite:///purse.db")


def test_a_missing_database_url_is_an_error_not_a_default() -> None:
    with pytest.raises(DatabaseUrlError):
        database_url(env={})
    with pytest.raises(DatabaseUrlError):
        database_url(env={"DATABASE_URL": "   "})


def test_database_url_reads_the_environment_mapping() -> None:
    assert database_url(env={"DATABASE_URL": "postgres://u:p@h/db"}).startswith(
        "postgresql+psycopg://"
    )


# -- migrations ---------------------------------------------------------------


def test_migration_scripts_are_packaged_next_to_the_code() -> None:
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "versions" / "c1_1_baseline.py").is_file()


def test_alembic_config_can_be_built_without_the_ini_file() -> None:
    config = alembic_config("postgresql://u:p@h/db")
    assert config.get_main_option("script_location") == str(MIGRATIONS_DIR)
    assert config.get_main_option("sqlalchemy.url", "").startswith("postgresql+psycopg://")


def test_the_migration_chain_is_linear_and_complete() -> None:
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(alembic_config("postgresql://u:p@h/db"))
    revisions = [script.revision for script in scripts.walk_revisions()]
    assert revisions == [
        "c1_8_audit_seq",
        "c1_7_audit_log",
        "c1_6_apis",
        "c1_5_skills",
        "c1_4_memories_current",
        "c1_3_memories",
        "c1_2_connections",
        "c1_1_baseline",
    ]


def test_the_migration_and_the_constant_agree_on_the_embedding_width() -> None:
    """A change to EMBEDDING_DIM without an ALTER migration must not pass CI.

    The DB-backed twin of this test
    (``tests/db/test_schema.py::test_embedding_dimension_matches_the_constant``)
    checks the deployed column; this one catches it without a database.
    """
    from purse.db.migrations.versions.c1_3_memories import EMBEDDING_DIM_AT_C1_3

    assert EMBEDDING_DIM_AT_C1_3 == EMBEDDING_DIM, (
        "purse.db.models.EMBEDDING_DIM changed but no migration altered "
        "memories.embedding. Write the ALTER migration, then update the "
        "expectation in the newest migration."
    )


# -- models -------------------------------------------------------------------


def test_the_current_view_mirrors_the_memories_columns_exactly() -> None:
    """The view mapping cannot drift from the table it derives from."""
    assert [c.name for c in MemoryCurrent.__table__.columns] == [
        c.name for c in Memory.__table__.columns
    ]


def test_the_view_is_marked_so_autogenerate_skips_it() -> None:
    assert Base.metadata.tables["memories_current"].info.get("purse_view") is True
    assert Base.metadata.tables["memories"].info.get("purse_view") is None


def test_every_table_is_registered() -> None:
    assert {
        "users",
        "workspaces",
        "connections",
        "oauth_clients",
        "memories",
        "memories_current",
        "skills",
        "skill_heads",
        "apis",
        "audit_log",
    } == set(Base.metadata.tables)


def test_oauth_client_metadata_column_is_named_metadata_in_sql() -> None:
    """`metadata` is reserved on a declarative class; the column is not."""
    from purse.db.models import OAuthClient

    assert "metadata" in OAuthClient.__table__.columns
    assert OAuthClient.client_metadata.key == "client_metadata"


# -- export contract ----------------------------------------------------------


def test_key_material_columns_are_classified_as_never_exported() -> None:
    assert {"key_ciphertext", "dek_wrapped"} == export_module.API_NEVER_EXPORTED_FIELDS
    assert not (export_module.API_EXPORTED_FIELDS & export_module.API_NEVER_EXPORTED_FIELDS)


def test_every_api_column_has_an_export_decision() -> None:
    """Adding a column to `apis` must force a yes/no on exporting it."""
    classified = export_module.API_EXPORTED_FIELDS | export_module.API_NEVER_EXPORTED_FIELDS
    assert set(Api.__table__.columns.keys()) == classified


def test_token_hash_is_classified_as_never_exported() -> None:
    assert "token_hash" in export_module.CONNECTION_NEVER_EXPORTED_FIELDS
    assert "token_hash" in Connection.__table__.columns


def test_export_format_version_is_declared() -> None:
    assert export_module.EXPORT_FORMAT == "purse.vault.export"
    assert export_module.EXPORT_FORMAT_VERSION == "1.0"


# -- repository shape ---------------------------------------------------------


def test_repo_exposes_no_way_to_ask_for_another_workspace() -> None:
    import inspect

    for name, member in inspect.getmembers(Repo, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        assert "workspace_id" not in inspect.signature(member).parameters, name


def test_content_hash_is_sha256_hex() -> None:
    digest = content_hash("# save policy")
    assert len(digest) == 64
    assert digest == content_hash("# save policy")
    assert digest != content_hash("# save policy ")


# -- test harness contract ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False)],
)
def test_require_db_flag_parsing(value: str, expected: bool) -> None:
    """CI sets REQUIRE_DB=1 so a skipped database test fails the build."""
    assert database_required({"REQUIRE_DB": value}) is expected
