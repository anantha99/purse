"""C1.5, C1.6, C1.7: skill versioning, API storage, audit trail."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from purse.db.models import Connection
from purse.db.repo import Repo, content_hash

pytestmark = pytest.mark.db


# -- skills -------------------------------------------------------------------


def test_a_skill_version_is_content_addressed(session: Session, repo: Repo) -> None:
    skill = repo.put_skill(name="purse-save-policy", version="1.0.0", content="# save policy")
    session.flush()
    assert skill.content_hash == content_hash("# save policy")


def test_the_head_follows_the_latest_write(session: Session, repo: Repo) -> None:
    """``put_skill`` moves the head to what it just wrote.

    Semver ordering is the caller's business (C5.3); the head is a pointer,
    not a max().
    """
    repo.put_skill(name="purse-save-policy", version="1.0.0", content="v1")
    repo.put_skill(name="purse-save-policy", version="1.1.0", content="v2")
    session.flush()

    head = repo.get_skill("purse-save-policy")
    assert head is not None
    assert head.version == "1.1.0"
    # History stays fetchable — that is what an export carries.
    old = repo.get_skill("purse-save-policy", version="1.0.0")
    assert old is not None
    assert old.content == "v1"
    assert len(repo.list_skill_versions("purse-save-policy")) == 2


def test_a_version_can_be_stored_without_moving_the_head(session: Session, repo: Repo) -> None:
    repo.put_skill(name="s", version="2.0.0", content="two")
    repo.put_skill(name="s", version="1.0.0", content="one", make_head=False)
    session.flush()
    head = repo.get_skill("s")
    assert head is not None
    assert head.version == "2.0.0"


def test_duplicate_workspace_name_version_is_rejected(session: Session, repo: Repo) -> None:
    repo.put_skill(name="s", version="1.0.0", content="one")
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        repo.put_skill(name="s", version="1.0.0", content="different content")
        session.flush()


def test_list_skills_returns_heads_only(session: Session, repo: Repo) -> None:
    repo.put_skill(name="a", version="1.0.0", content="a1")
    repo.put_skill(name="a", version="1.1.0", content="a2")
    repo.put_skill(name="b", version="0.1.0", content="b1")
    session.flush()
    assert [(s.name, s.version) for s in repo.list_skills()] == [("a", "1.1.0"), ("b", "0.1.0")]


def test_frontmatter_round_trips_as_jsonb(session: Session, repo: Repo) -> None:
    repo.put_skill(
        name="s",
        version="1.0.0",
        content="body",
        frontmatter={"name": "s", "description": "d", "version": "1.0.0", "tags": ["x"]},
    )
    session.flush()
    head = repo.get_skill("s")
    assert head is not None
    assert head.frontmatter["tags"] == ["x"]


# -- apis ---------------------------------------------------------------------


def test_api_storage_round_trips(session: Session, repo: Repo) -> None:
    repo.add_api(
        name="stripe",
        provider="Stripe",
        base_url="https://api.stripe.com",
        auth_style="bearer",
        allowed_hosts=["api.stripe.com"],
        key_ciphertext=b"\x00ciphertext\x01",
        dek_wrapped=b"\x02wrapped-dek\x03",
    )
    session.flush()
    api = repo.get_api("stripe")
    assert api is not None
    assert api.allowed_hosts == ["api.stripe.com"]
    assert api.key_ciphertext == b"\x00ciphertext\x01"
    assert api.rotated_at is None


def test_rotation_stamps_rotated_at(session: Session, repo: Repo) -> None:
    repo.add_api(
        name="stripe",
        provider="Stripe",
        base_url="https://api.stripe.com",
        auth_style="bearer",
        key_ciphertext=b"old",
        dek_wrapped=b"old-dek",
    )
    session.flush()
    assert repo.rotate_api_key("stripe", key_ciphertext=b"new", dek_wrapped=b"new-dek") is True
    session.flush()
    api = repo.get_api("stripe")
    assert api is not None
    assert api.key_ciphertext == b"new"
    assert api.rotated_at is not None


def test_duplicate_api_name_in_a_workspace_is_rejected(session: Session, repo: Repo) -> None:
    repo.add_api(name="stripe", provider="Stripe", base_url="https://a", auth_style="bearer")
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        repo.add_api(name="stripe", provider="Stripe", base_url="https://b", auth_style="bearer")
        session.flush()


# -- audit --------------------------------------------------------------------


def test_audit_entries_come_back_newest_first(
    session: Session, repo: Repo, connection: Connection
) -> None:
    for index in range(3):
        repo.record_audit(
            connection_id=connection.id,
            action="add_memory",
            target_type="memory",
            target_id=f"target-{index}",
            agent_id="claude-code",
        )
    session.flush()
    entries = repo.list_audit(limit=2)
    assert len(entries) == 2
    assert entries[0].target_id == "target-2"
    assert entries[0].agent_id == "claude-code"


def test_revoking_a_connection_stamps_revoked_at(
    session: Session, repo: Repo, connection: Connection
) -> None:
    assert repo.revoke_connection(connection.id) is True
    session.flush()
    revoked = repo.get_connection(connection.id)
    assert revoked is not None
    assert revoked.revoked_at is not None
    # Revoking twice is a no-op, not an error.
    assert repo.revoke_connection(connection.id) is False
