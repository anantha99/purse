"""App factory for the ``/web`` dashboard, wired from the environment (C7).

:func:`create_web_router` is what :mod:`purse.gateway.asgi` mounts at ``/web`` on
the one Purse ASGI app. It reads ``PURSE_OWNER_PASSWORD`` and
``PURSE_SESSION_SECRET`` at construction and builds a :class:`SessionManager`
from them — but never *requires* them: if ``PURSE_OWNER_PASSWORD`` is unset the
routes still mount and boot succeeds; ``/web/login`` simply returns a clear
``LOGIN_DISABLED`` error. That is the whole point of resolving config here rather
than raising: the dashboard being unconfigured must not take down ``/mcp`` or
``/v1`` on the same app.

Named a "router" because that is the role it plays for the orchestrator (a
mountable unit of the ``/web`` surface); it is concretely a FastAPI sub-app, so
it can carry its own structured-error exception handlers — mirroring how
:mod:`purse.gateway.rest` is mounted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import FastAPI
from sqlalchemy.orm import Session

from purse.memory.engine import MemoryEngine
from purse.web.routes import create_web_app
from purse.web.session import SessionManager

__all__ = ["create_web_router"]


def create_web_router(
    session_factory: Callable[[], Session],
    engine: MemoryEngine,
    *,
    env: Mapping[str, str] | None = None,
    sessions: SessionManager | None = None,
) -> FastAPI:
    """Build the mountable ``/web`` app.

    :param session_factory: One session per request (committed on success).
    :param engine: The shared memory engine.
    :param env: Overrides ``os.environ`` for config resolution (tests).
    :param sessions: An explicit :class:`SessionManager` (tests inject one with a
        known password + secret). When omitted it is built from the environment.
    """
    manager = sessions if sessions is not None else SessionManager.from_env(env)
    return create_web_app(session_factory, engine, sessions=manager)
