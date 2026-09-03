# Spike C3.4b — Mem0 pgvector ranking verification + adapter recipe

**Date:** 2026-09-03
**Engine under test:** `mem0ai==2.0.19` (installed, imports clean on Python 3.13; runtime pin is 3.12 per C0.4)
**Scope:** verify the ranking bug (issue #6883), produce the exact embedded-adapter config, confirm workspace isolation / canonical-id round-trip / async-failure / rebuild semantics. **No adapter built** — that is C3.4.

## Verdict: **GO** (no workaround needed for ranking)

The feared ranking inversion (#6883) is **NOT present in 2.0.19**. Mem0's pgvector store orders by true cosine distance in SQL *and* converts distance to a proper similarity before the Python re-rank; both stages agree on nearest-first. The real blockers are mundane and all handled below: a missing `psycopg-pool` dependency, an LLM key required at construction even with `infer=False`, telemetry on by default, and the two-place `dims` config.

---

## 1. Dependency footprint

`uv add mem0ai==2.0.19` pulls **21 transitive packages**. Declared runtime deps of mem0ai 2.0.19:

| Package | Notes |
|---|---|
| `openai==3.7.0` | embedder + (unused, see §4) LLM client. Uses `httpx2`. |
| `qdrant-client==1.19.0` | **Hard dep even though we use pgvector** — mem0's default store, not optional in 2.0.19. Drags in `grpcio==1.83.1`, `numpy==2.5.2`, `protobuf==6.33.6`, `portalocker`→`pywin32`. ~15 MB of weight we never execute. |
| `posthog==7.45.3` | **Telemetry** — phones home to `us.i.posthog.com` on add/search/delete unless disabled (§6). Pulls `backoff`, `distro`, `requests`. |
| `httpx==0.28.1` | mem0/qdrant pin httpx **0.x**; the repo deliberately uses `httpx2` for tests. They are separate distributions and coexist, but the core env now carries both. |
| `pydantic==2.13.5`, `pytz`, `protobuf`, `sqlalchemy` (already ours) | — |

**Not pulled but REQUIRED for the pgvector store:** `psycopg-pool`. `mem0/vector_stores/pgvector.py` does `from psycopg_pool import ConnectionPool` at module load (psycopg3 path); without it the import falls through to psycopg2 (also absent) and raises `ImportError`. **Added `psycopg-pool>=3.2` to `pyproject.toml` alongside `mem0ai==2.0.19`.**

- **Compat:** no Python 3.12/3.14 problem observed; imports fine on the local 3.13 venv. Keep the 3.12 runtime pin from C0.4.
- **History store:** `infer=False` still writes a per-memory row to a **SQLite** history DB (`self.db.add_history`, `main.py:1981`), default `~/.mem0/history.db`. We never read it; it is a portability/concurrency wart in a multi-worker deploy. Configurable via `history_db_path` (set it to a writable, per-instance path, or accept the default). Noted in C3.4.
- **Slim-down (optional, later):** `qdrant-client`, `posthog` are dead weight for us; there is no extra in 2.0.19 to exclude them. Not a blocker.

---

## 2. THE CRITICAL QUESTION — ranking bug #6883: **ABSENT / FIXED in 2.0.19**

Traced end-to-end through the actually-installed source. Three stages, all consistent:

### Stage 1 — the store's SQL and score (`mem0/vector_stores/pgvector.py`, `PGVector.search`)

```python
# lines 356-362
SELECT id, vector <=> %s::vector AS distance, payload
FROM {}
{}
ORDER BY distance
LIMIT %s
# line 367
return [OutputData(id=str(r[0]), score=max(0.0, 1.0 - float(r[1])), payload=r[2]) for r in results]
```

- `<=>` is pgvector's **cosine distance** operator (0 = identical, 1 = orthogonal, 2 = opposite).
- `ORDER BY distance` (ascending) ⇒ **nearest first**, computed in the database. Correct.
- `score = max(0.0, 1.0 - distance)` ⇒ distance is converted to a **similarity** (higher = better) *before* leaving the store. The `score` field is never raw distance.

### Stage 2 — the search path (`mem0/memory/main.py`, `_search_vector_store`)

```python
# line 1642
semantic_results = self.vector_store.search(query=query, vectors=embeddings, top_k=internal_limit, filters=filters)
# lines 1673-1677 — candidate carries the *similarity* through unchanged
candidates.append({"id": mem_id, "score": mem.score, "payload": payload})
# line 1680
scored_results = score_and_rank(semantic_results=candidates, ...)
```

### Stage 3 — the re-rank (`mem0/utils/scoring.py`, `score_and_rank`)

```python
# lines 110-112 — threshold GATES on the similarity (default threshold 0.1)
semantic_score = result.get("score") or 0.0
if semantic_score < threshold:
    continue
# line 138 — final ordering
scored.sort(key=lambda x: x["score"], reverse=True)   # DESCENDING by similarity
```

`reverse=True` on a **similarity** = nearest first. The threshold filters out *far* memories (low similarity), which is the desired behaviour, not the inverted one.

### Why #6883 does not bite here

The bug requires raw cosine *distance* to reach `score_and_rank` and be sorted as if larger = better. It cannot: `PGVector.search` already maps `distance → 1 - distance` at line 367, and additionally does the primary ordering in SQL (`ORDER BY distance`). There is **no** `score_and_rank` variant in 2.0.19 that sees a distance. `grep` for any other sort/`ORDER BY`/`reverse=` in the ranking path finds only these. **Verdict: absent. No workaround, no version pin beyond `==2.0.19`, no post-rank re-sort needed in our adapter.**

> Executable guard added: `tests/memory/test_mem0_ranking_db.py` (db-marked; skips locally, runs in CI vs pgvector pg17). It drives `PGVector` directly with hand-chosen vectors and asserts the identical/near vectors outrank the orthogonal ones and that scores descend — a red bar there is exactly the #6883 symptom returning. It also asserts the workspace-filter isolation (§3) and the canonical-id payload round-trip.

---

## 3. Config recipe

### The dict (what `Memory.from_config` receives)

```python
import os
from mem0 import Memory

config = {
    "vector_store": {
        "provider": "pgvector",
        "config": {
            # Point at OUR Postgres (DATABASE_URL). Plain libpq URI — strip any
            # SQLAlchemy "+psycopg" driver suffix before handing it to mem0.
            "connection_string": os.environ["DATABASE_URL"],
            # Separate table — MUST NOT be "memories" (our canonical table).
            "collection_name": "purse_mem0",
            # CRITICAL: set explicitly (see silent-data-loss note below).
            "embedding_model_dims": 1536,
            "hnsw": True,      # ANN index; fine for our scale
            "diskann": False,
        },
    },
    "embedder": {
        "provider": "openai",   # OpenAI-compatible; works against Ollama/LM Studio via base_url
        "config": {
            "model": "text-embedding-3-small",
            "embedding_dims": 1536,          # CRITICAL, see below — different key name than the store's!
            "api_key": os.environ["PURSE_EMBED_API_KEY"],
            "openai_base_url": os.environ.get("PURSE_EMBED_BASE_URL", "https://api.openai.com/v1"),
        },
    },
    "llm": {
        # Never invoked (we always call add(..., infer=False)), but the OpenAI
        # client is constructed in Memory.__init__ and RAISES without a key.
        # Supply a placeholder so construction succeeds offline / key-free.
        "provider": "openai",
        "config": {"api_key": os.environ.get("PURSE_EMBED_API_KEY", "sk-unused-infer-false"),
                   "model": "gpt-4o-mini"},
    },
}

mem = Memory.from_config(config)   # verified to construct offline (pool opens non-blocking; table is lazy)
```

### Write (verbatim, no LLM)

```python
mem.add(
    record.content,                       # stored VERBATIM under payload["data"]
    user_id=str(workspace_id),            # <-- workspace scope (see §3 isolation)
    infer=False,                          # no extraction/rewrite; pure vector index
    metadata={"purse_memory_id": str(record.id)},   # canonical id for the round trip
)
```

### Search (rank via Mem0, map back to canonical rows)

```python
res = mem.search(
    query,
    top_k=limit,
    filters={"user_id": str(workspace_id)},   # REQUIRED — see isolation below
    # threshold defaults to 0.1 (gates on cosine similarity); tune if verbatim
    # facts fall below it.
)
for hit in res["results"]:
    canonical_id = hit["metadata"]["purse_memory_id"]   # -> hydrate from Postgres
    score = hit["score"]
```

### The two-place `dims` — the "silent data loss" flag (confirmed real)

There are **two independent keys**, easy to set one and forget the other:
- vector store: `vector_store.config.embedding_model_dims` — `PGVectorConfig` **defaults to 1536** (`mem0/configs/vector_stores/pgvector.py:9`) and is used to `CREATE TABLE … vector({dims})`.
- embedder: `embedder.config.embedding_dims` — `BaseEmbedderConfig` (`.../configs/embeddings/base.py:80`). If **unset**, `OpenAIEmbedding` sets `_pass_dimensions_to_api = False` (`.../embeddings/openai.py:18`) and does **not** send `dimensions` to the API — so a matryoshka model returns its *native* width. If that native width ≠ the table's 1536, inserts fail (best case) or, with a silently-1536 model, you get a dimensionality you never intended. **Our recipe sets BOTH to 1536 explicitly.** Verified offline: `store dims=1536, embedder dims=1536, _pass_dimensions_to_api=True`. This matches our `memories.embedding` column (`EMBEDDING_DIM = 1536`, `purse/db/models.py:90`).

### Table coexistence

Mem0 lazily creates its collection table (`id UUID PK, vector vector(1536), payload JSONB`) plus a GIN index on `payload->>'text_lemmatized'` and an HNSW index, on first insert/search (`_ensure_collection` → `create_col`, `pgvector.py:262`). It runs `CREATE EXTENSION IF NOT EXISTS vector` (already present in our image/migrations). It is a **separate table** and coexists cleanly with our own — **provided `collection_name` is not `memories`** (default is `mem0`; we use `purse_mem0`). Caveat: it lives **outside Alembic** (mem0 owns its DDL), so it will appear in the DB unmanaged by our migrations. Acceptable — the index is "derived, droppable" by design (§8.2), and C3.6 rebuilds it.

---

## 3b. Workspace isolation — **metadata filter, NOT a table boundary** (CRITICAL)

Mem0's pgvector store keeps **all workspaces in one shared collection table**. Isolation is purely a `payload->>'user_id'` predicate:
- `add(user_id=X)` stores `X` into the payload *and* strips any `user_id` a caller smuggled through freeform `metadata` (`_build_filters_and_metadata`, `main.py:360-378`), so metadata cannot place a memory into an unrequested scope.
- `search(filters={"user_id": X})` compiles to SQL `WHERE payload->>%s = %s` (`_build_filter_conditions`, `pgvector.py:113`).
- **`search()` raises `ValueError` if no `user_id`/`agent_id`/`run_id` is in `filters`** (`main.py:1457-1461`) — so a totally unscoped search is impossible, but a *wrong* scope is on us.

**Adapter rule:** map `user_id = str(workspace_id)` on every `add` and `search`. There is no per-workspace table; the filter is the entire isolation mechanism. The regression test asserts this (two workspaces, identical vectors, filter returns only its own). This mirrors our canonical-side invariant (C1.8) but is *not* structurally enforced by Mem0 — the adapter must never call Mem0 search without the workspace filter.

## 3c. Canonical-id round-trip

- **Out:** `add(..., metadata={"purse_memory_id": str(record.id)})`. Stored verbatim in `payload` (`_create_memory`, `main.py:1968-1969`). Mem0 mints its *own* uuid for the vector row (`main.py:1967`) — we do not control it and do not need to; the canonical id lives in the payload.
- **Back:** search results surface non-core payload keys under `result["metadata"]` (`main.py:1721-1725`), so `hit["metadata"]["purse_memory_id"]` is our canonical uuid. The adapter returns `EngineHit(memory_id=<that uuid>, score=hit["score"])`; the service hydrates the canonical row from Postgres and drops anything no longer current (existing `search_memory` logic). We **never** surface Mem0's `payload["data"]` to users (it could be a rewrite in an `infer=True` world; with `infer=False` it is verbatim, but the canonical row stays the source of truth regardless).

---

## 4. Async + failure semantics

- **Call shape:** `mem.add(...)` and `mem.search(...)` are **synchronous** functions. C3.4 runs `add` in a background task (the existing `_ingest` is best-effort). Fits the `MemoryEngine.ingest(record, *, workspace_id)` signature directly.
- **Failures raise catchably.** Embedder HTTP errors surface as `openai` exceptions; store errors surface as psycopg exceptions; `_get_cursor` rolls back and re-raises (`pgvector.py:238-241`). Nothing is swallowed inside mem0's add path. This is exactly what `purse.memory.service._ingest` wants — it wraps the call in `try/except Exception`, logs at warning, and the canonical write (already committed) is never lost. **Confirmed compatible with the "engine failure must never fail a canonical write" rule.**
- **Construction caveat:** `Memory(config)` builds the OpenAI **LLM** client eagerly (`main.py:499`) and it raises without an api_key *even though `infer=False` never calls it*. Recipe supplies a placeholder llm key. (Reproduced: without it, `openai.OpenAIError: Missing credentials` at construction.)
- **Pool lifecycle:** the psycopg pool is created with `open=False, wait=False` (non-blocking startup, good for Docker races). On process exit with an unreachable DB, worker threads emit `couldn't stop thread … within 5.0s` noise. In production the DB is live so it is moot; long-running server should own/close the pool (or pass a `connection_pool`) to keep shutdown clean.

## 5. Rebuild (C3.6)

- **drop(workspace):** `mem.delete_all(user_id=str(workspace_id))` — deletes every row matching the filter, batched (`main.py:1890-1944`). (`reset()` would nuke ALL workspaces — never use it for a per-workspace drop.)
- **rebuild(workspace, records):** `delete_all(user_id=…)` then replay the canonical current-view stream through `add(content, user_id=…, infer=False, metadata={"purse_memory_id": …})`. Deterministic and idempotent because `infer=False` never consults the LLM or dedupes — one canonical row in ⇒ one vector row out. Proves "derived, droppable, rebuildable" (§8.2, §3.1-2).

## 6. Telemetry (must disable)

`MEM0_TELEMETRY` defaults to `"True"` (`mem0/memory/telemetry.py:14`); add/search/delete fire PostHog events to `us.i.posthog.com`. For a privacy-positioned vault this must be off. **Set `MEM0_TELEMETRY=false` in the app environment** (compose + Fly). Belt-and-braces: it is read at import, so set it before importing mem0.

---

## Overall: **GO**

Ranking is correct in `mem0ai==2.0.19` — no patch, no workaround, no reliance on the pgvector fallback (C3.5) for correctness. Ship the adapter (C3.4) against the recipe above. Required, non-negotiable adapter details, all verified here:

1. Add `psycopg-pool>=3.2` (done) — the pgvector store won't import without it.
2. Set **both** `embedding_model_dims` (store) and `embedding_dims` (embedder) to **1536**.
3. `collection_name="purse_mem0"` — never `"memories"`.
4. `user_id=str(workspace_id)` on every `add`/`search`; never search without the workspace filter (isolation is a metadata predicate, not a table).
5. `infer=False` on every `add`; supply a placeholder `llm.api_key` so construction succeeds key-free.
6. `MEM0_TELEMETRY=false` in the environment.
7. Carry the canonical id in `metadata["purse_memory_id"]`; rank via Mem0, return canonical rows.

Regression guard: `tests/memory/test_mem0_ranking_db.py` (CI, pgvector pg17).
