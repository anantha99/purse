"""Database configuration.

Configuration is environment variables only (PRD §6): no config file, no
defaults that would let a deployment boot against the wrong database.

Two variables are read:

``DATABASE_URL``
    The connection string the application uses.
``TEST_DATABASE_URL``
    Optional override used by the test suite so that running the tests never
    points at a real deployment by accident. Tests prefer it over
    ``DATABASE_URL``.

Both accept the plain ``postgresql://`` form that Postgres tooling, compose
files, and hosting providers emit; :func:`normalize_database_url` rewrites the
driver to ``postgresql+psycopg`` (psycopg 3) so the rest of the code never has
to care which spelling arrived.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from sqlalchemy.engine import make_url

__all__ = [
    "DATABASE_URL_ENV",
    "DRIVER",
    "TEST_DATABASE_URL_ENV",
    "database_url",
    "normalize_database_url",
]

DATABASE_URL_ENV = "DATABASE_URL"
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

#: The DBAPI Purse standardises on. psycopg 3, installed as ``psycopg[binary]``.
DRIVER = "psycopg"

_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", f"postgresql+{DRIVER}"})


class DatabaseUrlError(RuntimeError):
    """Raised when the database URL is missing or is not a Postgres URL."""


def normalize_database_url(raw: str) -> str:
    """Return *raw* with the driver pinned to psycopg 3.

    ``postgres://`` and ``postgresql://`` both become ``postgresql+psycopg://``.
    A URL that already names psycopg is returned unchanged. Anything that is not
    Postgres is rejected loudly — Purse's schema is pgvector-specific and there
    is no meaningful SQLite fallback.
    """
    url = make_url(raw)
    if url.drivername not in _POSTGRES_SCHEMES:
        raise DatabaseUrlError(
            f"unsupported database URL scheme {url.drivername!r}: "
            f"Purse requires Postgres (with the pgvector extension)"
        )
    if url.drivername != f"postgresql+{DRIVER}":
        url = url.set(drivername=f"postgresql+{DRIVER}")
    return url.render_as_string(hide_password=False)


def database_url(env: Mapping[str, str] | None = None) -> str:
    """Return the normalized application database URL from the environment.

    Raises :class:`DatabaseUrlError` if ``DATABASE_URL`` is unset or empty —
    booting against an implicit localhost default is exactly the failure mode
    the fail-fast rule in the compose file exists to prevent.
    """
    source = os.environ if env is None else env
    raw = (source.get(DATABASE_URL_ENV) or "").strip()
    if not raw:
        raise DatabaseUrlError(
            f"{DATABASE_URL_ENV} is not set. Purse reads its database connection "
            f"string from the environment only; see .env.example."
        )
    return normalize_database_url(raw)
