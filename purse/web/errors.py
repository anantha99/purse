"""Structured errors for the ``/web`` operator surface (C7).

The web dashboard speaks the same wire shape as the rest of Purse — PRD §10's
``{"error": {"code", "message"}}`` — but it owns two codes the agent surfaces
never emit:

* ``UNAUTHENTICATED`` — no session token, or one that is forged, tampered, or
  expired. Distinct from the agent gateway's ``UNAUTHORIZED`` (a PAT/OAuth
  bearer that did not resolve) because the web contract names it so
  (``docs/web-api-contract.md``) and the BFF uses it to decide when to bounce
  the browser back to ``/login``.
* ``INVALID_CREDENTIALS`` — a login attempt with the wrong operator password.
  Deliberately separate from ``UNAUTHENTICATED`` so the login screen can tell
  "your password is wrong" from "your session expired".

``LOGIN_DISABLED`` is an operational state, not in §10's list: the instance was
booted without ``PURSE_OWNER_PASSWORD`` (or ``PURSE_SESSION_SECRET``), so there
is no password to check against. It is a 503 — the feature is unavailable, not
the request malformed — and it never crashes boot (the routes still mount).
"""

from __future__ import annotations

__all__ = [
    "InvalidCredentialsError",
    "LoginDisabledError",
    "NotFoundError",
    "UnauthenticatedError",
    "ValidationError",
    "WebError",
]


class WebError(Exception):
    """An error with a stable wire ``code`` and an HTTP ``status``."""

    code: str = "WEB_ERROR"
    status: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnauthenticatedError(WebError):
    """No session token, or one that is forged, tampered, or expired.

    The message stays vague at the wire: distinguishing "no token" from "expired
    token" from "bad signature" is a probing oracle, and the BFF only needs to
    know the session is not usable.
    """

    code = "UNAUTHENTICATED"
    status = 401


class InvalidCredentialsError(WebError):
    """A login attempt with the wrong operator password."""

    code = "INVALID_CREDENTIALS"
    status = 401


class LoginDisabledError(WebError):
    """Login is not configured on this instance (no password/secret in the env)."""

    code = "LOGIN_DISABLED"
    status = 503


class NotFoundError(WebError):
    """A referenced resource does not exist in the operator's workspace."""

    code = "NOT_FOUND"
    status = 404


class ValidationError(WebError):
    """A request parameter is missing, malformed, or out of range."""

    code = "VALIDATION"
    status = 422
