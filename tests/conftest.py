"""Shared pytest fixtures.

The database tests need a real Postgres with pgvector — the whole point of C1
is behaviour that Postgres enforces (append-only triggers, composite foreign
keys, a view), none of which a fake would prove. So:

* ``TEST_DATABASE_URL`` (preferred) or ``DATABASE_URL`` points at a live
  server. The tests never touch that database directly: each run creates a
  throwaway one, migrates it, and drops it afterwards.
* With neither set, the DB tests **skip** with a clear message, and the unit
  tests still run.
* With ``REQUIRE_DB=1`` set, a skip becomes a **failure**. CI sets it, so a
  misconfigured service container can never look like a green build.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import NoReturn

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from purse.db.config import DATABASE_URL_ENV, TEST_DATABASE_URL_ENV, normalize_database_url
from purse.db.migrate import upgrade

REQUIRE_DB_ENV = "REQUIRE_DB"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def database_required(env: dict[str, str] | None = None) -> bool:
    """True when a skipped database test must be treated as a failure."""
    source = os.environ if env is None else env
    return source.get(REQUIRE_DB_ENV, "").strip().lower() in _TRUTHY


def _unavailable(reason: str) -> NoReturn:
    """Skip, unless the environment insists the database must be there."""
    if database_required():
        pytest.fail(
            f"{REQUIRE_DB_ENV} is set, so the database tests must run, but they cannot: {reason}"
        )
    pytest.skip(reason)


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """The URL of a live Postgres server to create throwaway databases on."""
    raw = (os.environ.get(TEST_DATABASE_URL_ENV) or os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not raw:
        _unavailable(
            f"no {TEST_DATABASE_URL_ENV} or {DATABASE_URL_ENV} in the environment. "
            f"Start one with: docker compose -f docker-compose.dev.yml up -d db "
            f"then export DATABASE_URL=postgresql://purse:<password>@localhost:5432/purse"
        )
    return normalize_database_url(raw)


@pytest.fixture(scope="session")
def test_database_url(postgres_url: str) -> Iterator[str]:
    """Create a throwaway database for this run and drop it afterwards.

    A whole database rather than a schema: ``CREATE EXTENSION vector`` is
    database-scoped, so a per-schema harness would have to share (or fight
    over) one extension installation between concurrent runs.
    """
    name = f"purse_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as connection:
            # The name is a locally generated uuid hex, not user input.
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except SQLAlchemyError as exc:
        admin.dispose()
        _unavailable(f"cannot reach Postgres at {make_url(postgres_url).render_as_string()}: {exc}")

    url = make_url(postgres_url).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def migrated_engine(test_database_url: str) -> Iterator[Engine]:
    """An engine on a freshly migrated throwaway database."""
    try:
        upgrade(test_database_url)
    except SQLAlchemyError as exc:  # pragma: no cover - only on a broken server
        _unavailable(f"migrations failed against the test database: {exc}")
    engine = create_engine(test_database_url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """A session wrapped in a transaction that is rolled back after each test.

    Tests share one migrated database and never see each other's rows.
    """
    connection = migrated_engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Memory / gateway fakes (C3)
# ---------------------------------------------------------------------------
#
# Appended rather than merged into the block above so the C1 fixtures keep their
# original shape and history. The imports are therefore not at the top of the
# file, which is what the `noqa: E402` markers are for — they support the
# append, they are not a lint the code failed on its merits.
#
# These live in the shared conftest because both `tests/memory` and
# `tests/gateway` need the same authenticated-caller stub and the same engine
# doubles, and pytest makes fixtures visible to both without an import.

import dataclasses  # noqa: E402
from collections.abc import Iterable  # noqa: E402

from purse.memory.engine import EngineHit, MemoryEngine  # noqa: E402
from purse.memory.records import MemoryRecord  # noqa: E402


@dataclasses.dataclass(frozen=True)
class StubContext:
    """An already-authenticated caller.

    Satisfies `purse.memory.context.WriteContext` and
    `purse.gateway.rest.GatewayContext` structurally — which is the point of
    those being Protocols. No `purse.auth` import, so the memory and gateway
    suites stay green while C2 is still being built in parallel.
    """

    connection_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_id: str | None = None
    scopes: tuple[str, ...] = ("memory:read", "memory:write")
    writes_enabled: bool = True


class RecordingEngine(MemoryEngine):
    """Remembers every call and returns whatever hits it was handed.

    Lets a test assert that the canonical path *reached* the engine, and drive
    the engine-first branch of `search_memory` without a real index.
    """

    def __init__(self, hits: list[EngineHit] | None = None) -> None:
        self.hits = hits if hits is not None else []
        self.ingested: list[MemoryRecord] = []
        self.searched: list[tuple[uuid.UUID, str, int]] = []
        self.forgotten: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.rebuilt: list[uuid.UUID] = []
        self.dropped: list[uuid.UUID] = []

    def ingest(self, record: MemoryRecord, *, workspace_id: uuid.UUID) -> None:
        self.ingested.append(record)

    def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[EngineHit]:
        self.searched.append((workspace_id, query, limit))
        return list(self.hits[:limit])

    def forget(self, workspace_id: uuid.UUID, memory_id: uuid.UUID) -> None:
        self.forgotten.append((workspace_id, memory_id))

    def rebuild(self, workspace_id: uuid.UUID, records: Iterable[MemoryRecord]) -> None:
        self.rebuilt.append(workspace_id)
        list(records)

    def drop(self, workspace_id: uuid.UUID) -> None:
        self.dropped.append(workspace_id)


class EngineFailure(RuntimeError):
    """Distinctive enough that a test can tell it apart from a real bug."""


class RaisingEngine(MemoryEngine):
    """Every method explodes. The engine an outage looks like.

    PRD §8.2 / C3.3: an engine failure must never fail a canonical write. This
    is how that is proved rather than asserted.
    """

    def ingest(self, record: MemoryRecord, *, workspace_id: uuid.UUID) -> None:
        raise EngineFailure("ingest exploded")

    def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[EngineHit]:
        raise EngineFailure("search exploded")

    def forget(self, workspace_id: uuid.UUID, memory_id: uuid.UUID) -> None:
        raise EngineFailure("forget exploded")

    def rebuild(self, workspace_id: uuid.UUID, records: Iterable[MemoryRecord]) -> None:
        raise EngineFailure("rebuild exploded")

    def drop(self, workspace_id: uuid.UUID) -> None:
        raise EngineFailure("drop exploded")


@pytest.fixture
def recording_engine() -> RecordingEngine:
    return RecordingEngine()


@pytest.fixture
def raising_engine() -> RaisingEngine:
    return RaisingEngine()
