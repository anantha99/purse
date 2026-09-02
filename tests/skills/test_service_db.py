"""The skills service against a real Postgres (C5.2-C5.4) — db-marked.

Skipped locally (no Postgres); run in CI where ``REQUIRE_DB=1`` turns a skip into
a failure. It has to be a real database: the ``(workspace, name, version)`` unique
key, the ``skill_heads`` pointer, and the workspace isolation the composite keys
enforce are all behaviours of the schema.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from purse.db.repo import Repo
from purse.skills import service
from purse.skills.errors import NotFoundError, ValidationError
from purse.skills.seed import seed_default_skills
from tests.conftest import StubContext

pytestmark = pytest.mark.db


def _doc(name: str, version: str, *, body: str = "body text") -> str:
    return f"---\nname: {name}\ndescription: A {name} skill.\nversion: {version}\n---\n{body}\n"


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


def test_upsert_creates_a_version_and_points_the_head(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    record = service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0"))
    assert record.name == "deploy"
    assert record.version == "1.0.0"
    assert record.description == "A deploy skill."
    assert record.body.strip() == "body text"

    stored = repo.get_skill("deploy")
    assert stored is not None
    assert stored.version == "1.0.0"
    assert stored.content_hash == record.content_hash


def test_upsert_writes_an_audit_row_naming_the_skill(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0"))
    entries = repo.list_audit()
    assert len(entries) == 1
    assert entries[0].action == "skill.upsert"
    assert entries[0].target_type == "skill"
    assert entries[0].target_id == "deploy"
    assert entries[0].connection_id == ctx.connection_id


def test_reupsert_identical_content_is_idempotent(
    session: Session, ctx: StubContext, repo: Repo
) -> None:
    doc = _doc("deploy", "1.0.0")
    first = service.upsert_skill(session, ctx, name="deploy", content=doc)
    second = service.upsert_skill(session, ctx, name="deploy", content=doc)

    assert second.content_hash == first.content_hash
    # Exactly one stored version, and only the first write was audited.
    assert len(repo.list_skill_versions("deploy")) == 1
    assert len(repo.list_audit()) == 1


def test_same_version_different_content_is_rejected(session: Session, ctx: StubContext) -> None:
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0", body="one"))
    with pytest.raises(ValidationError, match="bump the version"):
        service.upsert_skill(
            session, ctx, name="deploy", content=_doc("deploy", "1.0.0", body="two")
        )


def test_a_new_version_bumps_the_head(session: Session, ctx: StubContext, repo: Repo) -> None:
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0"))
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "2.0.0"))

    head = repo.get_skill("deploy")
    assert head is not None
    assert head.version == "2.0.0"
    # Both versions are retained as history.
    assert {s.version for s in repo.list_skill_versions("deploy")} == {"1.0.0", "2.0.0"}


def test_frontmatter_name_must_match_the_argument(session: Session, ctx: StubContext) -> None:
    with pytest.raises(ValidationError, match="does not match"):
        service.upsert_skill(session, ctx, name="deploy", content=_doc("release", "1.0.0"))


# ---------------------------------------------------------------------------
# get / list
# ---------------------------------------------------------------------------


def test_get_skill_latest_vs_specific_version(session: Session, ctx: StubContext) -> None:
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0", body="v1"))
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "2.0.0", body="v2"))

    latest = service.get_skill(session, ctx, name="deploy")
    assert latest.version == "2.0.0"
    assert latest.body.strip() == "v2"

    pinned = service.get_skill(session, ctx, name="deploy", version="1.0.0")
    assert pinned.version == "1.0.0"
    assert pinned.body.strip() == "v1"


def test_list_skills_shows_the_latest_per_name(session: Session, ctx: StubContext) -> None:
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0"))
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "2.0.0"))
    service.upsert_skill(session, ctx, name="review", content=_doc("review", "0.1.0"))

    summaries = {s.name: s for s in service.list_skills(session, ctx)}
    assert set(summaries) == {"deploy", "review"}
    assert summaries["deploy"].version == "2.0.0"
    assert summaries["review"].version == "0.1.0"
    assert summaries["deploy"].description == "A deploy skill."


def test_get_missing_skill_is_not_found(session: Session, ctx: StubContext) -> None:
    with pytest.raises(NotFoundError):
        service.get_skill(session, ctx, name="nope")


def test_get_missing_version_is_not_found(session: Session, ctx: StubContext) -> None:
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0"))
    with pytest.raises(NotFoundError):
        service.get_skill(session, ctx, name="deploy", version="9.9.9")


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


def test_skills_are_isolated_per_workspace(
    session: Session, ctx: StubContext, other_ctx: StubContext
) -> None:
    service.upsert_skill(session, ctx, name="deploy", content=_doc("deploy", "1.0.0"))

    # Invisible to the other workspace.
    assert service.list_skills(session, other_ctx) == []
    with pytest.raises(NotFoundError):
        service.get_skill(session, other_ctx, name="deploy")

    # And a same-named skill in the other workspace does not collide.
    service.upsert_skill(session, other_ctx, name="deploy", content=_doc("deploy", "5.0.0"))
    assert service.get_skill(session, ctx, name="deploy").version == "1.0.0"
    assert service.get_skill(session, other_ctx, name="deploy").version == "5.0.0"


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def test_seed_default_skills_loads_the_save_policy(session: Session, ctx: StubContext) -> None:
    seed_default_skills(session, workspace_id=ctx.workspace_id, connection_id=ctx.connection_id)
    names = {s.name for s in service.list_skills(session, ctx)}
    assert "purse-save-policy" in names

    policy = service.get_skill(session, ctx, name="purse-save-policy")
    assert policy.version == "1.0.0"
    assert policy.description
    assert policy.body.strip()


def test_seed_default_skills_is_idempotent(session: Session, ctx: StubContext, repo: Repo) -> None:
    seed_default_skills(session, workspace_id=ctx.workspace_id, connection_id=ctx.connection_id)
    seed_default_skills(session, workspace_id=ctx.workspace_id, connection_id=ctx.connection_id)

    versions = repo.list_skill_versions("purse-save-policy")
    assert len(versions) == 1
    # The second run wrote nothing, so only the first seed was audited.
    assert len(repo.list_audit()) == 1
