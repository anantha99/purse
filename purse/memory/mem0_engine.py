"""The Mem0 OSS derived-index adapter (C3.4-C3.6, PRD §8.2).

This is the real :class:`~purse.memory.engine.MemoryEngine` behind the seam the
service already talks to. It wraps ``mem0ai==2.0.19`` configured as a **pure
vector index over our own Postgres/pgvector** — no LLM extraction, no rewriting,
no consolidation. Every ``add`` is ``infer=False``: the canonical content goes in
verbatim, the canonical id rides along in ``metadata["purse_memory_id"]``, and
recall ranks by cosine similarity and hands back *pointers*, which the service
hydrates from the canonical store (PRD §8.2: "Mem0's rewrites never touch
canonical rows").

The exact configuration recipe — and the reasons each knob is set the way it is —
was verified in ``docs/spikes/mem0-ranking-spike.md`` (C3.4b). The load-bearing
points, all confirmed against the installed source:

* **``collection_name="purse_mem0"``**, never ``"memories"`` — Mem0 owns its own
  table (``id UUID, vector, payload JSONB``), created lazily on first write,
  living beside our canonical table, outside Alembic (derived, droppable).
* **``embedding_model_dims`` (store) *and* ``embedding_dims`` (embedder) both set
  to 1536** — two independent keys; leaving the embedder one unset silently drops
  the ``dimensions`` API parameter and can corrupt the index width.
* **Workspace isolation is a metadata predicate, not a table boundary.** Every
  ``add``/``search``/``delete_all`` carries ``user_id=str(workspace_id)``; Mem0
  keeps all workspaces in one collection and scopes by ``payload->>'user_id'``.
  The adapter must never call search without the filter.
* **A placeholder LLM key is required at construction.** ``Memory.__init__`` builds
  an OpenAI LLM client eagerly and raises without a key, even though ``infer=False``
  never calls it.
* **Telemetry off.** ``MEM0_TELEMETRY`` defaults on and phones home on every
  add/search/delete; set to ``false`` before Mem0 is imported.

Failures raise honestly. The service wraps every engine call in ``try`` (see
:mod:`purse.memory.engine`), so an embedding provider outage or a store hiccup
degrades recall, never a canonical write.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import make_url

from purse.memory.engine import EngineHit, MemoryEngine
from purse.memory.records import MemoryRecord

# Mem0 reads MEM0_TELEMETRY at *import* time (mem0/memory/telemetry.py). Set it
# before anything in this process imports mem0 — belt to the environment's braces
# (compose/Fly also set it). ``setdefault`` so an operator who deliberately set it
# is respected.
os.environ.setdefault("MEM0_TELEMETRY", "false")

logger = logging.getLogger(__name__)

__all__ = [
    "COLLECTION_NAME",
    "EMBEDDING_DIM",
    "EmbeddingConfig",
    "Mem0Engine",
    "build_memory_engine_from_env",
]

#: Mem0's collection table name. MUST NOT be ``"memories"`` (our canonical table).
COLLECTION_NAME = "purse_mem0"

#: The embedding width. Matches ``purse.db.models.EMBEDDING_DIM`` and the
#: ``memories.embedding`` column, so a future pgvector fallback (C3.5) shares it.
EMBEDDING_DIM = 1536

#: The payload key carrying the canonical id across the Mem0 round trip.
PURSE_MEMORY_ID_KEY = "purse_memory_id"

#: Mem0 builds an OpenAI LLM client eagerly and raises without a key, even though
#: ``infer=False`` never invokes it. This placeholder makes construction succeed
#: key-free; it is never used to reach a network.
_PLACEHOLDER_LLM_KEY = "sk-unused-infer-false"

# ---------------------------------------------------------------------------
# Env vars for the engine factory (documented once, here)
# ---------------------------------------------------------------------------
#: The presence of a non-empty key here is what *enables* the Mem0 engine. Without
#: it, :func:`build_memory_engine_from_env` returns a ``NullEngine`` and the
#: service keeps its ILIKE fallback — staging without an embedding provider still
#: works, it just has no semantic recall.
EMBEDDING_API_KEY_ENV = "PURSE_EMBEDDING_API_KEY"
EMBEDDING_BASE_URL_ENV = "PURSE_EMBEDDING_BASE_URL"
EMBEDDING_MODEL_ENV = "PURSE_EMBEDDING_MODEL"
EMBEDDING_DIMS_ENV = "PURSE_EMBEDDING_DIMS"
EMBEDDING_PROVIDER_ENV = "PURSE_EMBEDDING_PROVIDER"

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "text-embedding-3-small"
_DEFAULT_PROVIDER = "openai"


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """How to reach an OpenAI-compatible embedding endpoint.

    Pluggable by design (PRD §8.2, "engine is swappable"): point *base_url* at
    Ollama, LM Studio, vLLM, Voyage, or any OpenAI-compatible server. *provider*
    is Mem0's embedder provider name — ``"openai"`` in production; tests register a
    deterministic fake provider and pass its name here so the db-marked suite runs
    against real pgvector without a network or a key.
    """

    api_key: str
    model: str = _DEFAULT_MODEL
    base_url: str = _DEFAULT_BASE_URL
    dims: int = EMBEDDING_DIM
    provider: str = _DEFAULT_PROVIDER


def _plain_conninfo(database_url: str) -> str:
    """A libpq URI psycopg accepts, stripped of any SQLAlchemy driver suffix.

    Our ``DATABASE_URL`` is normalised to ``postgresql+psycopg://…``; Mem0's
    pgvector store hands the string straight to psycopg, which wants
    ``postgresql://…``.
    """
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class Mem0Engine(MemoryEngine):
    """Mem0 as a verbatim vector index over Purse's own pgvector (C3.4).

    Construction is cheap and offline (the psycopg pool opens non-blocking, the
    collection table is created lazily on first write), but the underlying
    ``Memory`` object is still built once and reused: it owns a connection pool and
    a history handle. Construction is guarded by a lock so the multi-threaded ASGI
    app builds exactly one, and ``add``/``search`` are Mem0-synchronous and
    thread-safe over the pool thereafter.
    """

    def __init__(
        self,
        *,
        embedding: EmbeddingConfig,
        database_url: str,
        history_db_path: str | None = None,
    ) -> None:
        self._embedding = embedding
        self._conninfo = _plain_conninfo(database_url)
        self._history_db_path = history_db_path
        self._lock = threading.Lock()
        self._memory: Any = None

    # -- configuration ------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        """The exact ``Memory.from_config`` dict from the C3.4b spike recipe."""
        config: dict[str, Any] = {
            "vector_store": {
                "provider": "pgvector",
                "config": {
                    "connection_string": self._conninfo,
                    "collection_name": COLLECTION_NAME,
                    "embedding_model_dims": self._embedding.dims,
                    "hnsw": True,
                    "diskann": False,
                },
            },
            "embedder": {
                "provider": self._embedding.provider,
                "config": {
                    "model": self._embedding.model,
                    # BOTH dim keys are set (this one and the store's above) — the
                    # spike's "silent data loss" flag.
                    "embedding_dims": self._embedding.dims,
                    "api_key": self._embedding.api_key,
                    "openai_base_url": self._embedding.base_url,
                },
            },
            "llm": {
                # Never invoked (every add is infer=False) but constructed eagerly.
                "provider": "openai",
                "config": {"api_key": _PLACEHOLDER_LLM_KEY, "model": "gpt-4o-mini"},
            },
        }
        if self._history_db_path is not None:
            config["history_db_path"] = self._history_db_path
        return config

    def _client(self) -> Any:
        """The lazily-built, reused ``Memory`` instance (double-checked lock)."""
        if self._memory is None:
            with self._lock:
                if self._memory is None:
                    # Imported here, not at module top: keep the heavy Mem0 import
                    # (qdrant, posthog, numpy) off the path of a NullEngine deploy
                    # and off the unit-test import of the factory.
                    os.environ.setdefault("MEM0_TELEMETRY", "false")
                    from mem0 import Memory

                    self._memory = Memory.from_config(self._config())
        return self._memory

    # -- MemoryEngine -------------------------------------------------------

    def ingest(self, record: MemoryRecord, *, workspace_id: uuid.UUID) -> None:
        """Index one canonical record verbatim, scoped to its workspace."""
        self._client().add(
            record.content,
            user_id=str(workspace_id),
            infer=False,
            metadata={PURSE_MEMORY_ID_KEY: str(record.id)},
        )

    def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[EngineHit]:
        """Rank this workspace's memories for *query*, nearest first.

        Returns pointers (canonical id + score), never content: the service
        hydrates from Postgres. The ``user_id`` filter is the entire isolation
        mechanism and is never omitted.
        """
        result = self._client().search(
            query,
            top_k=limit,
            filters={"user_id": str(workspace_id)},
        )
        hits: list[EngineHit] = []
        for item in result.get("results", []):
            canonical_id = _canonical_id(item)
            if canonical_id is None:
                # A row without our metadata is not ours to hand back (e.g. a
                # legacy row); skip rather than guess.
                continue
            hits.append(EngineHit(memory_id=canonical_id, score=item.get("score")))
        return hits

    def forget(self, workspace_id: uuid.UUID, memory_id: uuid.UUID) -> None:
        """Drop every Mem0 row carrying this canonical id in this workspace.

        Mem0 mints its own uuid per vector row, so we cannot delete by canonical
        id directly: we look the vector rows up by the ``purse_memory_id`` payload
        predicate (scoped to ``user_id``) and delete each by Mem0's id. Idempotent
        — no matches is a no-op.
        """
        client = self._client()
        found = client.get_all(
            filters={"user_id": str(workspace_id), PURSE_MEMORY_ID_KEY: str(memory_id)},
        )
        for item in found.get("results", []):
            vector_id = item.get("id")
            if vector_id is not None:
                client.delete(vector_id)

    def rebuild(self, workspace_id: uuid.UUID, records: Iterable[MemoryRecord]) -> None:
        """Drop this workspace's index and replay it from the canonical stream."""
        self.drop(workspace_id)
        for record in records:
            self.ingest(record, workspace_id=workspace_id)

    def drop(self, workspace_id: uuid.UUID) -> None:
        """Delete every Mem0 row for this workspace. Never ``reset()`` (all-workspaces)."""
        self._client().delete_all(user_id=str(workspace_id))


def _canonical_id(item: Mapping[str, Any]) -> uuid.UUID | None:
    """Pull our canonical uuid out of a Mem0 search hit's ``metadata``.

    Mem0 surfaces non-core payload keys under ``result["metadata"]`` (verified in
    the spike). A malformed or absent value yields ``None`` rather than raising —
    one bad row must not sink a whole search.
    """
    metadata = item.get("metadata") or {}
    raw = metadata.get(PURSE_MEMORY_ID_KEY)
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def build_memory_engine_from_env(
    *,
    env: Mapping[str, str] | None = None,
    database_url: str | None = None,
) -> MemoryEngine:
    """Return a :class:`Mem0Engine` when an embedding key is configured, else ``NullEngine``.

    The switch is deliberately the embedding **API key** (``PURSE_EMBEDDING_API_KEY``):
    a Mem0 engine with no way to embed is useless, so its absence is exactly the
    signal to fall back. Staging without a key keeps the canonical path and the
    ILIKE search working — no semantic recall, but a working vault.

    Env vars (all optional except the key):

    * ``PURSE_EMBEDDING_API_KEY``   — enables the engine when non-empty.
    * ``PURSE_EMBEDDING_BASE_URL``  — OpenAI-compatible base URL (default OpenAI).
    * ``PURSE_EMBEDDING_MODEL``     — embedding model (default ``text-embedding-3-small``).
    * ``PURSE_EMBEDDING_DIMS``      — embedding width (default 1536).
    * ``PURSE_EMBEDDING_PROVIDER``  — Mem0 embedder provider (default ``openai``).

    *database_url* overrides ``DATABASE_URL`` (the app passes its resolved URL);
    if neither is present the engine cannot reach a store, so this falls back to
    ``NullEngine`` with a warning rather than raising — a missing embedding setup
    must never stop the app from booting.
    """
    from purse.memory.engine import NullEngine

    source = os.environ if env is None else env
    api_key = (source.get(EMBEDDING_API_KEY_ENV) or "").strip()
    if not api_key:
        return NullEngine()

    resolved_db = database_url or source.get("DATABASE_URL")
    if not resolved_db or not resolved_db.strip():
        logger.warning(
            "%s is set but no DATABASE_URL is available; semantic recall is disabled "
            "and memory search falls back to canonical text search",
            EMBEDDING_API_KEY_ENV,
        )
        return NullEngine()

    dims_raw = (source.get(EMBEDDING_DIMS_ENV) or "").strip()
    try:
        dims = int(dims_raw) if dims_raw else EMBEDDING_DIM
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using the default %d",
            EMBEDDING_DIMS_ENV,
            dims_raw,
            EMBEDDING_DIM,
        )
        dims = EMBEDDING_DIM

    embedding = EmbeddingConfig(
        api_key=api_key,
        model=(source.get(EMBEDDING_MODEL_ENV) or "").strip() or _DEFAULT_MODEL,
        base_url=(source.get(EMBEDDING_BASE_URL_ENV) or "").strip() or _DEFAULT_BASE_URL,
        dims=dims,
        provider=(source.get(EMBEDDING_PROVIDER_ENV) or "").strip() or _DEFAULT_PROVIDER,
    )
    return Mem0Engine(embedding=embedding, database_url=resolved_db.strip())
