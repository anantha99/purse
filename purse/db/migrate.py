"""Programmatic access to the Alembic migration environment.

``alembic.ini`` at the repository root exists for the CLI (``uv run alembic
upgrade head``). It is not shipped in the wheel, so anything that has to run
migrations from installed code — the test harness, a self-host first-boot
path — builds the configuration here instead, from the package directory.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from purse.db.config import database_url, normalize_database_url

__all__ = ["MIGRATIONS_DIR", "alembic_config", "current_revision", "downgrade", "upgrade"]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str | None = None) -> Config:
    """Build an Alembic :class:`Config` pointed at Purse's migration scripts.

    *url* defaults to ``DATABASE_URL`` from the environment.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", normalize_database_url(url) if url else database_url())
    return config


def upgrade(url: str | None = None, revision: str = "head") -> None:
    """Apply migrations up to *revision* (default: the latest)."""
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    """Roll migrations back to *revision* (default: an empty schema)."""
    command.downgrade(alembic_config(url), revision)


def current_revision(url: str | None = None) -> None:
    """Print the revision the database is stamped at."""
    command.current(alembic_config(url))
