"""Skills service errors (C5.1-C5.3).

Mirrors :mod:`purse.memory.errors`: every error carries a **stable string
``code``**, and that code is the contract. PRD §10 fixes the structured error
shape ``{"error": {"code", "message"}}`` for the whole gateway surface; the REST
endpoints and the MCP tools both map these codes straight onto that envelope, so
a client that learned ``NOT_FOUND`` from the memory tools sees the identical code
from the skills tools.

Three codes are reachable from the skills service, and they are the same three
the memory service raises:

* ``VALIDATION`` — the skill document is malformed: no frontmatter, bad YAML, a
  missing/blank required field, a version that is not ``MAJOR.MINOR.PATCH``, a
  name that is not kebab-case, a frontmatter ``name`` that disagrees with the
  ``name`` argument, or a re-upsert of an existing version with different content.
* ``NOT_FOUND`` — ``get_skill`` was asked for a skill (or a specific version)
  that does not exist in this workspace.
* ``PAYLOAD_TOO_LARGE`` — the document exceeds the 64 KB inline limit (PRD §8.3).

``UNAUTHORIZED_SCOPE`` and ``RATE_LIMITED`` belong to the auth/gateway layer, so
they are deliberately not defined here — one owner per code.
"""

from __future__ import annotations

__all__ = [
    "NotFoundError",
    "PayloadTooLargeError",
    "SkillError",
    "ValidationError",
]


class SkillError(Exception):
    """Base class for every skills-service error.

    Exposes the same ``.code`` / ``.message`` pair as
    :class:`purse.memory.errors.MemoryError_`, so the gateway's error mapping can
    treat the two hierarchies identically.
    """

    #: Stable wire code. Subclasses override; never change one in place.
    code: str = "SKILL_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationError(SkillError):
    """The skill document is malformed, or a version collision was rejected."""

    code = "VALIDATION"


class NotFoundError(SkillError):
    """The referenced skill (or version) does not exist in this workspace.

    Deliberately does not distinguish "absent" from "belongs to another
    workspace": telling a caller an id exists somewhere else is a
    cross-workspace information leak (C1.8).
    """

    code = "NOT_FOUND"


class PayloadTooLargeError(SkillError):
    """The document exceeds the 64 KB inline limit (PRD §8.3)."""

    code = "PAYLOAD_TOO_LARGE"
