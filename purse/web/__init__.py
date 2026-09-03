"""The operator dashboard surface: session auth + the ``/web`` endpoints (C7).

Mounted at ``/web`` on the one Purse ASGI app (:mod:`purse.gateway.asgi`),
session-authenticated (:mod:`purse.web.session`), building to
``docs/web-api-contract.md``. Distinct from the agent surfaces ``/mcp`` and
``/v1`` — those are for agents, this is for the single human operator.
"""

from __future__ import annotations

from purse.web.app import create_web_router
from purse.web.session import SessionManager

__all__ = ["SessionManager", "create_web_router"]
