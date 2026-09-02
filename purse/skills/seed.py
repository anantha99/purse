"""Bundled seed skills, and the function that preloads them into a vault (C5.4).

Every new vault ships with the ``purse-save-policy`` skill — the product surface
that tells an agent what is worth saving to memory (PRD §8.2). The skill is a
real markdown artifact under :mod:`purse.skills.seeds`, versioned in-repo and
edited like any other document, not a string baked into Python.

:func:`seed_default_skills` upserts every bundled seed into a workspace. It is
**idempotent**: it goes through the same ``upsert_skill`` service path as any
other write, so re-seeding an already-seeded vault (a second boot, a re-run) is a
no-op by the content-hash rule — no duplicate rows, no error, no audit churn.

.. rubric:: Who calls this, and how

The orchestrator wires it into first-boot bootstrap (C3.7/C5.4). It is not called
here — this module only exposes the function — but the intended call site is
:func:`purse.auth.bootstrap.bootstrap`, right after the onboarding connection is
minted, using that connection as the provenance::

    from purse.skills import seed_default_skills

    result = bootstrap(session)  # existing
    seed_default_skills(
        session,
        workspace_id=result.workspace_id,
        connection_id=result.connection_id,
    )

The connection must belong to the workspace (the audit path enforces it), which
the onboarding connection does by construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from importlib import resources

from sqlalchemy.orm import Session

from purse.skills import service
from purse.skills.parse import parse_skill

__all__ = ["bundled_seed_skills", "seed_default_skills"]

#: The package directory holding the bundled ``*.md`` seed artifacts.
_SEEDS_PACKAGE = "purse.skills.seeds"


@dataclass(frozen=True, slots=True)
class _SeedContext:
    """A minimal :class:`~purse.skills.context.SkillContext` for the seed path.

    Bootstrap has a ``workspace_id`` and a ``connection_id`` but no assembled
    auth context, so this is the smallest object the service needs — the same two
    fields ``AuthContext`` would carry.
    """

    connection_id: uuid.UUID
    workspace_id: uuid.UUID


def bundled_seed_skills() -> list[tuple[str, str]]:
    """Every bundled seed as ``(name, content)``, sorted by name.

    The name is read from each document's own frontmatter (via the real parser),
    so it can never drift from what ``upsert_skill`` will require the argument to
    match.
    """
    seeds: list[tuple[str, str]] = []
    for entry in resources.files(_SEEDS_PACKAGE).iterdir():
        if entry.is_file() and entry.name.endswith(".md"):
            content = entry.read_text(encoding="utf-8")
            seeds.append((parse_skill(content).name, content))
    return sorted(seeds, key=lambda pair: pair[0])


def seed_default_skills(
    session: Session, *, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> None:
    """Upsert every bundled seed skill into *workspace_id*, idempotently.

    Uses the normal ``upsert_skill`` service path, so the content-hash rule makes
    a repeat run a no-op. Flushes but does not commit — the caller owns the
    transaction, exactly as the rest of the service layer does.
    """
    ctx = _SeedContext(connection_id=connection_id, workspace_id=workspace_id)
    for name, content in bundled_seed_skills():
        service.upsert_skill(session, ctx, name=name, content=content)
