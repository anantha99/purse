"""Auth failures and the structured error codes they carry (PRD §10).

Two rules shape this module.

**One message for every authentication failure.** ``authenticate_pat`` raises
:class:`AuthenticationError` with :data:`AUTHENTICATION_FAILED_MESSAGE` whether
the token was malformed, unknown, or belonged to a connection the user revoked
last week. Anything more helpful is an oracle: a caller that can tell "no such
token" from "revoked" can confirm a token *used to be* valid, which is exactly
the signal an attacker holding a leaked-and-since-revoked token wants.

**Authorisation failures, by contrast, are explicit.** By the time
:class:`ScopeError` is raised the caller has already proved who they are, so
telling them which scope they lack is help, not leakage — and it is what makes
a "your connection is read-only" message possible in a client UI.
"""

from __future__ import annotations

import enum

__all__ = [
    "AUTHENTICATION_FAILED_MESSAGE",
    "AuthError",
    "AuthenticationError",
    "ErrorCode",
    "ScopeError",
]


class ErrorCode(enum.StrEnum):
    """Machine-readable codes for the MCP ``{error: {code, message}}`` envelope.

    ``UNAUTHORIZED_SCOPE`` is from the PRD §10 list. ``UNAUTHENTICATED`` is not
    in that list because it is not a tool-level outcome: a request that never
    authenticated is rejected at the transport (HTTP 401) before any tool runs.
    It is defined here so the transport has one name for it too.
    """

    UNAUTHORIZED_SCOPE = "UNAUTHORIZED_SCOPE"
    UNAUTHENTICATED = "UNAUTHENTICATED"


#: The single, deliberately uninformative authentication failure message.
AUTHENTICATION_FAILED_MESSAGE = "invalid or revoked credentials"


class AuthError(Exception):
    """Base class for everything this package raises at a caller.

    Carries the structured code so the MCP and REST layers can map any auth
    failure to a response without knowing the subclass.
    """

    code: ErrorCode

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.code = code


class AuthenticationError(AuthError):
    """The credential could not be verified. Never says why.

    The message is fixed and the constructor takes no detail argument on
    purpose — there is no call site that can accidentally widen it, and no
    chance of a token fragment ending up in a traceback.
    """

    def __init__(self) -> None:
        super().__init__(AUTHENTICATION_FAILED_MESSAGE, code=ErrorCode.UNAUTHENTICATED)


class ScopeError(AuthError):
    """An authenticated caller lacks the scope (or the writes) for an action."""

    def __init__(self, message: str, *, scope: str) -> None:
        super().__init__(message, code=ErrorCode.UNAUTHORIZED_SCOPE)
        self.scope = scope
