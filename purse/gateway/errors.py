"""Transport-level gateway errors (C3.8, forward to C4.2).

PRD §10 fixes the wire shape for everything Purse returns when something goes
wrong::

    {"error": {"code": "...", "message": "..."}}

and names the codes: ``UNAUTHORIZED_SCOPE``, ``NOT_FOUND``, ``RATE_LIMITED``,
``HOST_NOT_ALLOWED``, ``PAYLOAD_TOO_LARGE``. The memory service owns three of
those (see :mod:`purse.memory.errors`). This module owns the two that happen
*before* any service is reached — authentication and scope — because they are
raised by code the gateway calls, not by the memory layer.

``UNAUTHORIZED`` is not in §10's list. §10 enumerates *tool* errors, which by
definition are raised after a caller was identified; a request with no usable
credential never gets that far and needs a code of its own. If C2 settles on a
different spelling, change it here — this is the only place it is written.
"""

from __future__ import annotations

__all__ = ["GatewayError", "RateLimitedError", "ScopeError", "UnauthorizedError"]


class GatewayError(Exception):
    """An error with a stable wire ``code`` and an HTTP ``status``."""

    code: str = "GATEWAY_ERROR"
    status: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnauthorizedError(GatewayError):
    """No credential, an unparseable one, or one that does not resolve to a connection.

    The ``authenticate`` callable handed to
    :func:`purse.gateway.rest.create_app` must raise this (and nothing else) for
    a bad token. Any other exception escaping it is treated as a server fault,
    on purpose: a database outage inside token lookup is a 500, and reporting it
    as "your token is invalid" would send users to rotate credentials that are
    fine.

    The message is deliberately vague at the wire. Distinguishing "unknown
    token" from "revoked token" from "expired token" is a probing oracle.
    """

    code = "UNAUTHORIZED"
    status = 401


class ScopeError(GatewayError):
    """The connection authenticated but lacks the scope this endpoint needs (PRD §10)."""

    code = "UNAUTHORIZED_SCOPE"
    status = 403


class RateLimitedError(GatewayError):
    """The connection exceeded its per-connection write budget (PRD §13, C2.10).

    ``retry_after`` is the seconds until the caller could retry, surfaced as an
    HTTP ``Retry-After`` header (rounded up) by the REST error handler. It is
    ``None`` only when a caller constructs the error without one.
    """

    code = "RATE_LIMITED"
    status = 429

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
