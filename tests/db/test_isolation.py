"""C1.8: a workspace is a wall, and every read path respects it.

Two workspaces in the same vault, written through their own repositories. Every
read the application has must return only its own rows — not "mostly", not
"unless you pass the wrong id", only.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from purse.db.models import AuthMode, InitiatedBy, MemoryKind
from purse.db.repo import NotFoundError, Repo, WorkspaceScopeError

from .conftest import TwoWorkspaces

pytestmark = pytest.mark.db


@pytest.fixture
def populated(session: Session, two_workspaces: TwoWorkspaces) -> TwoWorkspaces:
    """Identical-shaped data in both workspaces, distinguishable by content."""
    for label, repo, connection in (
        ("alpha", two_workspaces.alpha, two_workspaces.alpha_connection),
        ("beta", two_workspaces.beta, two_workspaces.beta_connection),
    ):
        live = repo.add_memory(
            content=f"{label} live memory",
            kind=MemoryKind.FACT,
            connection_id=connection.id,
            initiated_by=InitiatedBy.USER,
        )
        repo.supersede_memory(
            live.id,
            content=f"{label} superseded-then-current",
            connection_id=connection.id,
            initiated_by=InitiatedBy.AGENT,
        )
        buried = repo.add_memory(
            content=f"{label} tombstoned",
            kind=MemoryKind.DECISION,
            connection_id=connection.id,
            initiated_by=InitiatedBy.USER,
        )
        repo.tombstone_memory(buried.id)
        repo.put_skill(name=f"{label}-skill", version="1.0.0", content=f"# {label}")
        repo.add_api(
            name=f"{label}-api",
            provider=label,
            base_url=f"https://{label}.example.test",
            auth_style="bearer",
            allowed_hosts=[f"{label}.example.test"],
        )
        repo.record_audit(
            connection_id=connection.id,
            action="add_memory",
            target_type="memory",
            target_id=str(live.id),
        )
    session.flush()
    return two_workspaces


def test_memory_log_is_scoped(populated: TwoWorkspaces) -> None:
    assert all("alpha" in m.content for m in populated.alpha.list_memories())
    assert all("beta" in m.content for m in populated.beta.list_memories())
    assert len(populated.alpha.list_memories()) == 3
    assert len(populated.beta.list_memories()) == 3


def test_current_view_is_scoped(populated: TwoWorkspaces) -> None:
    assert {m.content for m in populated.alpha.current_memories()} == {
        "alpha superseded-then-current"
    }
    assert {m.content for m in populated.beta.current_memories()} == {
        "beta superseded-then-current"
    }


def test_skills_are_scoped(populated: TwoWorkspaces) -> None:
    assert [s.name for s in populated.alpha.list_skills()] == ["alpha-skill"]
    assert [s.name for s in populated.beta.list_skills()] == ["beta-skill"]
    assert populated.alpha.get_skill("beta-skill") is None
    assert [s.name for s in populated.alpha.list_skill_versions()] == ["alpha-skill"]


def test_apis_are_scoped(populated: TwoWorkspaces) -> None:
    assert [a.name for a in populated.alpha.list_apis()] == ["alpha-api"]
    assert [a.name for a in populated.beta.list_apis()] == ["beta-api"]
    assert populated.alpha.get_api("beta-api") is None


def test_audit_is_scoped(populated: TwoWorkspaces) -> None:
    alpha_entries = populated.alpha.list_audit()
    beta_entries = populated.beta.list_audit()
    assert len(alpha_entries) == 1
    assert len(beta_entries) == 1
    assert alpha_entries[0].connection_id == populated.alpha_connection.id
    assert beta_entries[0].connection_id == populated.beta_connection.id


def test_connections_are_scoped(populated: TwoWorkspaces) -> None:
    assert [c.client_name for c in populated.alpha.list_connections()] == ["cursor"]
    assert [c.client_name for c in populated.beta.list_connections()] == ["codex"]
    assert populated.alpha.get_connection(populated.beta_connection.id) is None


def test_get_memory_from_the_wrong_workspace_returns_nothing(
    populated: TwoWorkspaces,
) -> None:
    beta_memory = populated.beta.list_memories()[0]
    assert populated.alpha.get_memory(beta_memory.id) is None


def test_tombstoning_another_workspaces_memory_is_a_no_op(
    session: Session, populated: TwoWorkspaces
) -> None:
    # A *live* memory, deterministically: list_memories() returns the full log
    # incl. the tombstoned row, and every populated row shares a created_at
    # (one transaction), so list_memories()[0] tiebreaks on a random uuid and
    # can hand back the already-tombstoned memory. The current view excludes it.
    beta_memory = populated.beta.current_memories()[0]
    assert beta_memory.tombstone is False
    assert populated.alpha.tombstone_memory(beta_memory.id) is False
    session.flush()
    still_live = populated.beta.get_memory(beta_memory.id)
    assert still_live is not None
    assert still_live.tombstone is False


def test_writing_with_another_workspaces_connection_is_refused(
    populated: TwoWorkspaces,
) -> None:
    with pytest.raises(WorkspaceScopeError):
        populated.alpha.add_memory(
            content="smuggled",
            kind=MemoryKind.FACT,
            connection_id=populated.beta_connection.id,
            initiated_by=InitiatedBy.AGENT,
        )


def test_auditing_with_another_workspaces_connection_is_refused(
    populated: TwoWorkspaces,
) -> None:
    with pytest.raises(WorkspaceScopeError):
        populated.alpha.record_audit(
            connection_id=populated.beta_connection.id,
            action="use_api",
            target_type="api",
            target_id="beta-api",
        )


def test_superseding_across_workspaces_is_refused(populated: TwoWorkspaces) -> None:
    beta_memory = populated.beta.list_memories()[0]
    with pytest.raises(NotFoundError):
        populated.alpha.supersede_memory(
            beta_memory.id,
            content="cross-workspace rewrite",
            connection_id=populated.alpha_connection.id,
            initiated_by=InitiatedBy.AGENT,
        )


def test_the_database_itself_refuses_a_cross_workspace_supersession(
    session: Session, populated: TwoWorkspaces
) -> None:
    """Belt and braces: the composite foreign key, not just the repository.

    Raw SQL, bypassing every Python check.
    """
    alpha_connection = populated.alpha_connection
    beta_memory = populated.beta.list_memories()[0]
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            text(
                """
                INSERT INTO memories
                    (workspace_id, content, kind, supersedes, connection_id, initiated_by)
                VALUES
                    (:workspace_id, 'smuggled', 'fact', :supersedes, :connection_id, 'agent')
                """
            ),
            {
                "workspace_id": populated.alpha.workspace_id,
                "supersedes": beta_memory.id,
                "connection_id": alpha_connection.id,
            },
        )
    assert "fk_memories_supersedes_memories" in str(caught.value)


def test_repo_open_rejects_a_workspace_that_does_not_exist(session: Session) -> None:
    with pytest.raises(NotFoundError):
        Repo.open(session, uuid.uuid4())


def test_repo_takes_no_workspace_id_per_call() -> None:
    """The isolation guarantee is structural: there is no escape hatch to find.

    If any public method grows a ``workspace_id`` parameter, isolation stops
    being something the type of the object guarantees and starts being
    something reviewers have to notice.
    """
    offenders = []
    for name, member in inspect.getmembers(Repo, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = inspect.signature(member).parameters
        if "workspace_id" in parameters:
            offenders.append(name)
    assert offenders == []


def test_a_repo_is_bound_to_one_workspace(two_workspaces: TwoWorkspaces) -> None:
    assert two_workspaces.alpha.workspace_id != two_workspaces.beta.workspace_id
    assert two_workspaces.alpha.workspace().name == "Personal"
    assert two_workspaces.beta.workspace().name == "Work"


def test_connection_scopes_round_trip(session: Session, two_workspaces: TwoWorkspaces) -> None:
    connection = two_workspaces.alpha.add_connection(
        client_name="chatgpt",
        auth_mode=AuthMode.OAUTH_CIMD,
        scopes=["memory:read", "skills:read"],
        writes_enabled=False,
        token_hash=None,
    )
    session.flush()
    fetched = two_workspaces.alpha.get_connection(connection.id)
    assert fetched is not None
    assert fetched.scopes == ["memory:read", "skills:read"]
    assert fetched.auth_mode is AuthMode.OAUTH_CIMD
    assert fetched.writes_enabled is False
