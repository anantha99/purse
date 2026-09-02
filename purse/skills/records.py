"""Plain value objects the skills service hands back (C5.2).

Like :mod:`purse.memory.records`, these are frozen snapshots rather than
SQLAlchemy models: safe to return after the session closes, safe to hand to a
JSON encoder, and a surface that does not change when the schema does.

``workspace_id`` is deliberately absent from every shape on the wire — the
caller's connection already determines the workspace (PRD §10), so echoing it
back would be noise at best and a cross-workspace tell at worst.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from purse.db.models import Skill
from purse.skills.parse import ParsedSkill, extract_body

__all__ = ["SkillRecord", "SkillSummary"]


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """One resolved skill version: its frontmatter, its body, and its identity.

    Returned by ``get_skill`` and ``upsert_skill``. ``as_dict`` is the MCP/REST
    shape — frontmatter plus body plus the resolved version (PRD §10).
    """

    name: str
    version: str
    description: str
    body: str
    content: str
    content_hash: str
    created_at: dt.datetime
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_model(cls, skill: Skill) -> SkillRecord:
        """Snapshot a stored ``skills`` row, re-deriving the body from its content."""
        frontmatter = dict(skill.frontmatter or {})
        description = frontmatter.get("description", "")
        return cls(
            name=skill.name,
            version=skill.version,
            description=str(description) if description is not None else "",
            body=extract_body(skill.content),
            content=skill.content,
            content_hash=skill.content_hash,
            created_at=skill.created_at,
            frontmatter=frontmatter,
        )

    @classmethod
    def from_parsed(cls, parsed: ParsedSkill, *, created_at: dt.datetime) -> SkillRecord:
        """Snapshot a freshly parsed document (the idempotent-insert return path)."""
        return cls(
            name=parsed.name,
            version=parsed.version,
            description=parsed.description,
            body=parsed.body,
            content=parsed.content,
            content_hash=parsed.content_hash,
            created_at=created_at,
            frontmatter=parsed.frontmatter,
        )

    def as_dict(self) -> dict[str, Any]:
        """PRD §10 ``get_skill`` result: frontmatter + body, plus the resolved version."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "frontmatter": self.frontmatter,
            "body": self.body,
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """One row of ``list_skills``: the latest version of a skill (PRD §10)."""

    name: str
    description: str
    version: str

    @classmethod
    def from_model(cls, skill: Skill) -> SkillSummary:
        description = (skill.frontmatter or {}).get("description", "")
        return cls(
            name=skill.name,
            description=str(description) if description is not None else "",
            version=skill.version,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "version": self.version}
