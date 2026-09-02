"""Engine and session plumbing.

Nothing here is workspace-aware. Handing a bare :class:`~sqlalchemy.orm.Session`
around is exactly the escape hatch C1.8 exists to close, so application code
should take a :class:`purse.db.repo.Repo`, not a session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from purse.db.config import database_url, normalize_database_url

__all__ = ["create_db_engine", "session_factory", "session_scope"]


def create_db_engine(url: str | None = None, *, echo: bool = False, **kwargs: object) -> Engine:
    """Create an :class:`Engine`. *url* defaults to ``DATABASE_URL``.

    ``pool_pre_ping`` is on because self-hosted deployments sit behind
    connection-killing proxies and a stale pooled connection should be a
    reconnect, not a user-visible error.
    """
    resolved = normalize_database_url(url) if url else database_url()
    return create_engine(resolved, echo=echo, pool_pre_ping=True, future=True, **kwargs)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """A configured session factory bound to *engine*."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transaction scope: commit on success, roll back on any exception."""
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
