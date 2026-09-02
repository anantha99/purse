"""Alembic environment for Purse.

The URL is resolved in this order:

1. ``sqlalchemy.url`` set on the :class:`~alembic.config.Config` object by a
   programmatic caller (:mod:`purse.db.migrate`, which the tests use to point at
   a throwaway database).
2. ``DATABASE_URL`` from the environment.

There is no third fallback on purpose.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.schema import SchemaItem

from purse.db.config import database_url, normalize_database_url
from purse.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    configured = config.get_main_option("sqlalchemy.url", default=None)
    if configured:
        return normalize_database_url(configured)
    return database_url()


def include_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    """Keep views out of autogenerate.

    ``memories_current`` is mapped as an ORM class for read convenience, but it
    is a view created by a hand-written migration. Autogenerate must not try to
    CREATE TABLE it, or DROP it when reflecting.
    """
    return not (type_ == "table" and bool(obj.info.get("purse_view")))


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (``alembic upgrade --sql``)."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _do_run_migrations(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
