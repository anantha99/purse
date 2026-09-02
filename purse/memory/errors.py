"""Memory service errors (C3.1-C3.2).

Every error the memory service raises carries a **stable string ``code``**. That
code is the contract: PRD §10 specifies structured MCP errors shaped
``{"error": {"code", "message"}}``, and the REST gateway (C3.8) already emits
exactly that shape. When the MCP tools land (C4.2) they map these same codes,
so a client that learned ``NOT_FOUND`` from the REST smoke path sees the
identical code over MCP.

The codes here are the subset PRD §10 lists that the memory service itself can
raise. ``UNAUTHORIZED_SCOPE`` and ``RATE_LIMITED`` belong to the auth layer (C2)
and ``HOST_NOT_ALLOWED`` to the API proxy (C6) — they are deliberately not
defined here, so there is one owner per code.
"""

from __future__ import annotations

__all__ = [
    "MemoryError_",
    "NotFoundError",
    "PayloadTooLargeError",
    "ValidationError",
]


class MemoryError_(Exception):
    """Base class for every memory-service error.

    Named with a trailing underscore because ``MemoryError`` is a builtin, and
    shadowing it in a module that also does I/O would be a genuinely confusing
    bug to read. Import it as ``MemoryServiceError`` if that reads better at the
    call site — the alias is exported from :mod:`purse.memory`.
    """

    #: Stable wire code. Subclasses override; never change one in place.
    code: str = "MEMORY_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(MemoryError_):
    """The referenced memory does not exist, or is not current, in this workspace.

    Deliberately does not distinguish "absent" from "belongs to another
    workspace": telling a caller that an id exists somewhere else is a
    cross-workspace information leak (C1.8).
    """

    code = "NOT_FOUND"


class PayloadTooLargeError(MemoryError_):
    """Content exceeds the ``MAX_CONTENT_BYTES`` limit (PRD §10: ``add_memory`` ≤ 4 KB)."""

    code = "PAYLOAD_TOO_LARGE"


class ValidationError(MemoryError_):
    """A parameter is missing, empty, or not a member of its enum."""

    code = "VALIDATION"
