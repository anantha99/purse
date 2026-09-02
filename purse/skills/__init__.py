"""Skills (C5): versioned markdown playbooks — parsing, content-addressed versions, and history.

The split mirrors :mod:`purse.memory`:

:mod:`purse.skills.parse`
    Turns a markdown document with YAML frontmatter into a validated
    :class:`~purse.skills.parse.ParsedSkill`, or a ``VALIDATION`` /
    ``PAYLOAD_TOO_LARGE`` error. Owns the content address (sha256).
:mod:`purse.skills.service`
    The read/write path over :class:`purse.db.repo.Repo`. Applies the
    version/idempotency rule and audits writes. Every function is workspace-scoped
    by the caller's context and never commits.
:mod:`purse.skills.context`
    The structural "who is calling" contract — satisfied by ``purse.auth``'s real
    context without either package importing the other.
:mod:`purse.skills.errors`
    Stable error codes (``VALIDATION`` / ``NOT_FOUND`` / ``PAYLOAD_TOO_LARGE``),
    shared with the REST gateway and the MCP tools.
:mod:`purse.skills.seed`
    The bundled ``purse-save-policy`` skill (PRD §8.2) and the idempotent
    :func:`seed_default_skills` the orchestrator wires into bootstrap.
"""

from purse.skills.context import SkillContext
from purse.skills.errors import NotFoundError, PayloadTooLargeError, SkillError, ValidationError
from purse.skills.parse import (
    MAX_CONTENT_BYTES,
    ParsedSkill,
    extract_body,
    parse_skill,
)
from purse.skills.records import SkillRecord, SkillSummary
from purse.skills.seed import bundled_seed_skills, seed_default_skills
from purse.skills.service import get_skill, list_skills, upsert_skill

__all__ = [
    "MAX_CONTENT_BYTES",
    "NotFoundError",
    "ParsedSkill",
    "PayloadTooLargeError",
    "SkillContext",
    "SkillError",
    "SkillRecord",
    "SkillSummary",
    "ValidationError",
    "bundled_seed_skills",
    "extract_body",
    "get_skill",
    "list_skills",
    "parse_skill",
    "seed_default_skills",
    "upsert_skill",
]
