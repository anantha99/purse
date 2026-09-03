"""Executable regression guard for Mem0 issue #6883 (C3.4b spike).

The bug that was feared: Mem0's pgvector store returns cosine *distance* from the
``<=>`` operator but ranks as though it were *similarity*, so the WORST matches
surface first. If that were true here, semantic recall would be silently
inverted and every "top result" would be the least relevant memory in the vault.

This test drives ``mem0.vector_stores.pgvector.PGVector`` **directly** with
hand-chosen vectors — the component that owns the alleged defect (the
``ORDER BY`` on ``<=>`` and the ``score`` arithmetic in ``search``). Driving the
store rather than the full ``Memory`` API is the deliberate choice: it isolates
the ranking SQL from the embedder, the LLM, BM25 keyword search, and entity
extraction, so a red bar here means the ranking itself regressed and nothing
else. The vectors ARE the "deterministic fake embedder" — a fixed map from known
inputs to known vectors, applied at the store boundary.

Marked ``db``: it needs a real Postgres with pgvector (the throwaway database
from ``tests/conftest.py``). Skips locally without one; CI runs it against
pgvector ``pg17``. It also skips if ``mem0ai``/``psycopg_pool`` are not installed,
so it is inert until C3.4 lands those dependencies.

See ``docs/spikes/mem0-ranking-spike.md`` for the full verdict and the quoted
source lines this guards.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import make_url

# Inert until the C3.4 dependencies are present.
pytest.importorskip("mem0", reason="mem0ai not installed (C3.4 dependency)")
pytest.importorskip("psycopg_pool", reason="psycopg_pool not installed (pgvector store dependency)")

from mem0.vector_stores.pgvector import PGVector

pytestmark = pytest.mark.db

#: Tiny embedding width for hand-written vectors. The real index is 1536-dim
#: (``EMBEDDING_DIM``); the ranking arithmetic is width-independent, so 4 keeps
#: the fixtures readable.
_DIMS = 4


def _conninfo(sqlalchemy_url: str) -> str:
    """A plain libpq URI for psycopg, stripped of any SQLAlchemy driver suffix.

    ``tests/conftest`` hands out ``postgresql+psycopg://…``; psycopg's own
    connection string wants ``postgresql://…``.
    """
    return (
        make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


@pytest.fixture
def store(test_database_url: str) -> Iterator[PGVector]:
    """A fresh, uniquely named Mem0 pgvector collection, dropped after the test.

    A unique ``collection_name`` per test proves the incidental point that the
    index is a *separate table* that never collides with our canonical
    ``memories`` table, and keeps parallel tests from sharing rows.
    """
    collection = f"purse_mem0_rank_{uuid.uuid4().hex[:12]}"
    pg = PGVector(
        dbname=None,
        collection_name=collection,
        embedding_model_dims=_DIMS,
        user=None,
        password=None,
        host=None,
        port=None,
        diskann=False,
        hnsw=False,
        connection_string=_conninfo(test_database_url),
    )
    try:
        yield pg
    finally:
        # Best-effort teardown: the throwaway database is dropped regardless.
        with contextlib.suppress(Exception):
            pg.delete_col()


def _id() -> str:
    return str(uuid.uuid4())


def test_pgvector_search_returns_nearest_first(store: PGVector) -> None:
    """The nearest vector ranks first and the farthest ranks last (#6883 guard).

    Query is the unit vector ``[1,0,0,0]``. By construction the cosine
    similarity to the query is A=1.0 > B=0.6 > C=D=0.0, so a correct store
    returns A, then B, then the two orthogonal vectors — and the derived
    ``score`` (``1 - cosine_distance``) descends with it. If #6883 were present,
    A (identical) and B (near) would sink below C/D (orthogonal); the assertions
    below would flip.
    """
    a, b, c, d = _id(), _id(), _id(), _id()
    store.insert(
        vectors=[
            [1.0, 0.0, 0.0, 0.0],  # A — identical to the query
            [0.6, 0.8, 0.0, 0.0],  # B — unit vector, cosine 0.6 to the query
            [0.0, 0.0, 1.0, 0.0],  # C — orthogonal, cosine 0.0
            [0.0, 0.0, 0.0, 1.0],  # D — orthogonal, cosine 0.0
        ],
        payloads=[
            {"data": "A", "user_id": "ws"},
            {"data": "B", "user_id": "ws"},
            {"data": "C", "user_id": "ws"},
            {"data": "D", "user_id": "ws"},
        ],
        ids=[a, b, c, d],
    )

    results = store.search(query="", vectors=[1.0, 0.0, 0.0, 0.0], top_k=4)
    ranked_ids = [r.id for r in results]
    scores = [r.score for r in results]

    # Nearest-first: the identical vector leads, the near vector follows.
    assert ranked_ids[0] == a, f"nearest vector did not rank first: {ranked_ids}"
    assert ranked_ids[1] == b, f"second-nearest vector did not rank second: {ranked_ids}"

    # The farthest matches never outrank the nearest (the exact #6883 symptom).
    assert ranked_ids[0] not in {c, d}, (
        "an orthogonal (worst) match ranked first — ranking inverted"
    )

    # Scores are similarities in [0, 1], monotonically non-increasing, and match
    # the closed form score = max(0, 1 - cosine_distance).
    assert scores == sorted(scores, reverse=True), f"scores are not descending: {scores}"
    assert scores[0] == pytest.approx(1.0, abs=1e-6)
    assert scores[1] == pytest.approx(0.6, abs=1e-6)
    assert scores[-1] == pytest.approx(0.0, abs=1e-6)


def test_pgvector_search_is_workspace_scoped_by_filter(store: PGVector) -> None:
    """Isolation is a metadata filter, not a table boundary.

    Mem0 keeps every workspace's vectors in one collection and scopes reads by a
    ``payload->>'user_id'`` predicate. This proves our adapter MUST pass
    ``filters={"user_id": <workspace>}`` on every search — without it, the other
    workspace's identical vector comes straight back.
    """
    alpha, beta = _id(), _id()
    store.insert(
        vectors=[[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]],
        payloads=[
            {"data": "alpha secret", "user_id": "ws-alpha"},
            {"data": "beta secret", "user_id": "ws-beta"},
        ],
        ids=[alpha, beta],
    )

    hits = store.search(
        query="",
        vectors=[1.0, 0.0, 0.0, 0.0],
        top_k=10,
        filters={"user_id": "ws-alpha"},
    )
    returned = {r.id for r in hits}
    assert returned == {alpha}, "workspace filter leaked another workspace's memory"


def test_pgvector_search_round_trips_canonical_id_in_payload(store: PGVector) -> None:
    """The canonical id we stash in metadata comes back on the hit.

    This is the round trip the adapter relies on: rank via Mem0, then map the
    hit back to the canonical Postgres row by the id carried in the payload.
    """
    mem_id = _id()
    canonical_id = str(uuid.uuid4())
    store.insert(
        vectors=[[1.0, 0.0, 0.0, 0.0]],
        payloads=[{"data": "verbatim", "user_id": "ws", "purse_memory_id": canonical_id}],
        ids=[mem_id],
    )

    (hit,) = store.search(query="", vectors=[1.0, 0.0, 0.0, 0.0], top_k=1)
    assert hit.payload["purse_memory_id"] == canonical_id
