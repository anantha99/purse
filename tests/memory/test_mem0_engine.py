"""The Mem0 adapter's wiring, provable without a database or a network (C3.4).

Two things are checked here, both cheap: the engine **factory** picks the right
engine from the environment, and the **config recipe** the adapter hands Mem0 is
the one the C3.4b spike verified — right collection name, both ``dims`` keys set,
the placeholder LLM key, telemetry off, the driver suffix stripped. Constructing a
``Mem0Engine`` does none of this lazily-deferred work (no pool, no network), so
these run in the unit suite; the behaviour against real pgvector lives in
``test_mem0_engine_db.py``.
"""

from __future__ import annotations

import os

from purse.memory.engine import NullEngine
from purse.memory.mem0_engine import (
    COLLECTION_NAME,
    EMBEDDING_DIM,
    EmbeddingConfig,
    Mem0Engine,
    _plain_conninfo,
    build_memory_engine_from_env,
)

_FAKE_DB = "postgresql+psycopg://purse:secret@db.example:5432/purse"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_null_engine_without_a_key() -> None:
    """No embedding key ⇒ no semantic engine, and the service keeps its ILIKE path."""
    engine = build_memory_engine_from_env(env={"DATABASE_URL": _FAKE_DB})
    assert isinstance(engine, NullEngine)


def test_factory_returns_mem0_engine_with_a_key() -> None:
    """A key flips it on. Construction is offline — no pool opens here."""
    engine = build_memory_engine_from_env(
        env={"PURSE_EMBEDDING_API_KEY": "sk-real", "DATABASE_URL": _FAKE_DB},
    )
    assert isinstance(engine, Mem0Engine)


def test_factory_is_null_when_a_key_is_set_but_no_database_url() -> None:
    """A misconfigured engine must not stop the app booting — degrade, don't raise."""
    engine = build_memory_engine_from_env(env={"PURSE_EMBEDDING_API_KEY": "sk-real"})
    assert isinstance(engine, NullEngine)


def test_factory_database_url_argument_overrides_the_environment() -> None:
    engine = build_memory_engine_from_env(
        env={"PURSE_EMBEDDING_API_KEY": "sk-real"},
        database_url=_FAKE_DB,
    )
    assert isinstance(engine, Mem0Engine)


def test_factory_reads_embedding_overrides_from_env() -> None:
    engine = build_memory_engine_from_env(
        env={
            "PURSE_EMBEDDING_API_KEY": "sk-real",
            "PURSE_EMBEDDING_BASE_URL": "http://localhost:11434/v1",
            "PURSE_EMBEDDING_MODEL": "nomic-embed-text",
            "PURSE_EMBEDDING_DIMS": "768",
            "PURSE_EMBEDDING_PROVIDER": "ollama",
            "DATABASE_URL": _FAKE_DB,
        },
    )
    assert isinstance(engine, Mem0Engine)
    assert engine._embedding == EmbeddingConfig(
        api_key="sk-real",
        model="nomic-embed-text",
        base_url="http://localhost:11434/v1",
        dims=768,
        provider="ollama",
    )


def test_factory_defaults_are_openai_1536() -> None:
    engine = build_memory_engine_from_env(
        env={"PURSE_EMBEDDING_API_KEY": "sk-real", "DATABASE_URL": _FAKE_DB},
    )
    assert isinstance(engine, Mem0Engine)
    assert engine._embedding.provider == "openai"
    assert engine._embedding.model == "text-embedding-3-small"
    assert engine._embedding.base_url == "https://api.openai.com/v1"
    assert engine._embedding.dims == EMBEDDING_DIM


def test_factory_falls_back_to_default_dims_on_a_bad_value() -> None:
    engine = build_memory_engine_from_env(
        env={
            "PURSE_EMBEDDING_API_KEY": "sk-real",
            "PURSE_EMBEDDING_DIMS": "not-a-number",
            "DATABASE_URL": _FAKE_DB,
        },
    )
    assert isinstance(engine, Mem0Engine)
    assert engine._embedding.dims == EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Config recipe (the C3.4b spike, verbatim)
# ---------------------------------------------------------------------------


def _config() -> dict:
    engine = Mem0Engine(
        embedding=EmbeddingConfig(api_key="sk-real"),
        database_url=_FAKE_DB,
    )
    return engine._config()


def test_collection_name_is_not_the_canonical_table() -> None:
    config = _config()
    assert config["vector_store"]["config"]["collection_name"] == COLLECTION_NAME
    assert config["vector_store"]["config"]["collection_name"] != "memories"


def test_both_dimension_keys_are_set_to_1536() -> None:
    """The spike's 'silent data loss' flag: store *and* embedder dims, both 1536."""
    config = _config()
    assert config["vector_store"]["config"]["embedding_model_dims"] == 1536
    assert config["embedder"]["config"]["embedding_dims"] == 1536


def test_embedder_carries_base_url_and_key() -> None:
    config = _config()["embedder"]["config"]
    assert config["openai_base_url"] == "https://api.openai.com/v1"
    assert config["api_key"] == "sk-real"
    assert config["model"] == "text-embedding-3-small"


def test_llm_has_a_placeholder_key_so_construction_never_needs_a_real_one() -> None:
    """infer=False never calls the LLM, but Mem0 builds the client eagerly."""
    llm = _config()["llm"]
    assert llm["provider"] == "openai"
    assert llm["config"]["api_key"], "a placeholder key must be present"


def test_connection_string_is_a_plain_libpq_uri() -> None:
    """Mem0 hands the string to psycopg, which rejects the +psycopg suffix."""
    conn = _config()["vector_store"]["config"]["connection_string"]
    assert conn.startswith("postgresql://")
    assert "+psycopg" not in conn


def test_plain_conninfo_strips_the_sqlalchemy_driver() -> None:
    assert _plain_conninfo(_FAKE_DB).startswith("postgresql://")
    assert _plain_conninfo("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


def test_telemetry_is_disabled_by_importing_the_adapter() -> None:
    """Set at import time, before anything imports mem0 (spike §6)."""
    assert os.environ.get("MEM0_TELEMETRY") == "false"


def test_history_db_path_is_included_only_when_given() -> None:
    without = Mem0Engine(embedding=EmbeddingConfig(api_key="k"), database_url=_FAKE_DB)
    assert "history_db_path" not in without._config()

    with_path = Mem0Engine(
        embedding=EmbeddingConfig(api_key="k"),
        database_url=_FAKE_DB,
        history_db_path="/tmp/purse-mem0-history.db",  # noqa: S108 - test value, not a real path
    )
    assert with_path._config()["history_db_path"] == "/tmp/purse-mem0-history.db"  # noqa: S108
