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
