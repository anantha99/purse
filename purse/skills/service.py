"""The skills read and write path (PRD §8.3, §10; C5.1-C5.3).

Skills are versioned markdown playbooks. This module owns the service layer that
sits between the gateway (which authenticates and enforces scope) and
:class:`purse.db.repo.Repo` (which owns the ``skills`` / ``skill_heads`` tables):
it parses and validates a document, applies the version/idempotency rule, and
records the audit row for a write.

.. rubric:: The version / idempotency rule (C5.3)

``(workspace, name, version)`` is unique. An ``upsert_skill`` resolves to exactly
one of three outcomes:

1. **New version** — no row exists for this ``(name, version)``. A ``skills`` row
   is inserted and ``skill_heads`` is pointed at it, so it becomes the version
   ``get_skill(name)`` resolves to. "Latest" means *most recently upserted*, not
   the highest semver — the head is a pointer the writer moves, matching
   ``Repo.put_skill(make_head=True)``.
2. **Idempotent** — a row exists with the *same* ``content_hash``. The upsert is a
   no-op: the existing version is returned, no row is written, and nothing is
   audited. Re-running a bootstrap seed, or a client retrying, is therefore safe.
3. **Conflict** — a row exists with a *different* ``content_hash``. Rejected with
   ``VALIDATION``: an immutable version cannot be redefined, so the caller is told
   to bump the version.

.. rubric:: Provenance and audit

Only writes are audited (``skill.upsert``), and only when a row is actually
written — the idempotent path leaves no trail because nothing changed. The audit
target is the skill *name* (PRD §13: names and IDs only). ``connection_id`` is
the trusted provenance, exactly as for memory.

.. rubric:: On transactions

Like the memory service, these functions ``flush`` but never ``commit`` — the
caller owns the transaction boundary (the gateway commits per request, tests roll
back). The skill row and its audit row therefore land atomically.

.. rubric:: Scope

Scope enforcement lives in the gateway (``skills:read`` for reads,
``skills:write`` for ``upsert_skill``), not here — the same split the memory
service uses. By the time a call reaches this module the check has passed.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from purse.db.repo import NotFoundError as DbNotFoundError
from purse.db.repo import Repo
from purse.skills.context import SkillContext
from purse.skills.errors import NotFoundError, ValidationError
from purse.skills.parse import parse_skill
from purse.skills.records import SkillRecord, SkillSummary

__all__ = [
    "get_skill",
    "list_skills",
    "upsert_skill",
]

_AUDIT_ACTION = "skill.upsert"
_AUDIT_TARGET = "skill"


def _open_repo(session: Session, ctx: SkillContext) -> Repo:
    """The workspace-bound repository for this caller.

    ``Repo`` takes the workspace at construction and no method accepts one (C1.8),
    so from here on the isolation boundary is structural.
    """
    try:
        return Repo.open(session, ctx.workspace_id)
    except DbNotFoundError as exc:
        raise NotFoundError("workspace not found") from exc


def upsert_skill(session: Session, ctx: SkillContext, *, name: str, content: str) -> SkillRecord:
    """Create or update a skill from a markdown document (PRD §8.3, C5.3).

    Parses and validates *content*, requires that the frontmatter ``name`` equals
    the *name* argument, then applies the version/idempotency rule documented on
    this module. Returns the resolved version. Raises a skills
    :class:`~purse.skills.errors.SkillError` — ``VALIDATION`` for a malformed
    document, a name mismatch, or a version conflict; ``PAYLOAD_TOO_LARGE`` for an
    over-cap document.
    """
    parsed = parse_skill(content)
    if parsed.name != name:
        raise ValidationError(
            f"frontmatter name {parsed.name!r} does not match the skill name {name!r}"
        )

    repo = _open_repo(session, ctx)

    existing = repo.get_skill(name, version=parsed.version)
    if existing is not None:
        if existing.content_hash == parsed.content_hash:
            # Same (name, version, content): a genuine no-op. Return what is stored
            # without writing a row or an audit entry — this is what makes seeding
            # and client retries idempotent.
            return SkillRecord.from_model(existing)
        raise ValidationError(
            f"skill {name!r} version {parsed.version} already exists with different content "
            "— bump the version"
        )

    skill = repo.put_skill(
        name=name,
        version=parsed.version,
        content=content,
        frontmatter=parsed.frontmatter,
        make_head=True,
    )
    repo.record_audit(
        connection_id=ctx.connection_id,
        action=_AUDIT_ACTION,
        target_type=_AUDIT_TARGET,
        target_id=name,
    )
    return SkillRecord.from_model(skill)


def get_skill(
    session: Session, ctx: SkillContext, *, name: str, version: str | None = None
) -> SkillRecord:
    """Fetch a skill's frontmatter and body (PRD §10).

    ``version=None`` resolves the latest via ``skill_heads``; a specific version
    fetches exactly that row. A skill (or version) absent from this workspace is
    ``NOT_FOUND`` — the same answer whether it never existed or lives in another
    workspace, so the boundary leaks nothing.
    """
    repo = _open_repo(session, ctx)
    skill = repo.get_skill(name, version=version)
    if skill is None:
        if version is not None:
            raise NotFoundError(f"skill {name!r} has no version {version} in this workspace")
        raise NotFoundError(f"skill {name!r} not found in this workspace")
    return SkillRecord.from_model(skill)


def list_skills(session: Session, ctx: SkillContext) -> list[SkillSummary]:
    """The latest version of every skill in this workspace (PRD §10).

    Each entry is ``{name, description, version}``, ordered by name.
    """
    repo = _open_repo(session, ctx)
    return [SkillSummary.from_model(skill) for skill in repo.list_skills()]
