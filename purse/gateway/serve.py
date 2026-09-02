"""ASGI entrypoint for serving Purse (self-host / Fly / compose).

``uvicorn purse.gateway.serve:app`` builds the whole gateway from the
environment (``PURSE_PUBLIC_URL``, ``PURSE_OAUTH_SECRET``, ``DATABASE_URL``).
Kept as a module-level ``app`` so process managers can import it by string;
the Mem0 memory engine (C3.4) will be selected here once it lands.
"""

from __future__ import annotations

from purse.gateway.asgi import create_purse_app_from_env

app = create_purse_app_from_env()
"""The ASGI application object uvicorn serves."""
