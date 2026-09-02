"""C1.9: the vault export is complete, and it leaks nothing.

"Exit is a feature" (PRD §8.1) cuts both ways — everything the user wrote must
be in the file, and nothing that would let someone else use their credentials
may be.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from sqlalchemy.orm import Session

from purse.db.export import EXPORT_FORMAT, EXPORT_FORMAT_VERSION, export_vault
from purse.db.models import EMBEDDING_DIM, AuthMode, InitiatedBy, MemoryKind, User
from purse.db.repo import Repo, create_workspace

pytestmark = pytest.mark.db

# A recognisable byte pattern, so a leak is unmistakable rather than plausible.
CIPHERTEXT = b"CIPHERTEXT-MUST-NEVER-LEAVE-THE-VAULT"
WRAPPED_DEK = b"WRAPPED-DEK-MUST-NEVER-LEAVE-THE-VAULT"
# A stand-in for a stored PAT hash. Not a credential: it is a fixed hex string
# that only ever exists inside a rolled-back test transaction.
TOKEN_HASH = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105


@pytest.fixture
def vault(session: Session, user: User) -> User:
    """A vault with two workspaces and one of everything worth exporting."""
    for name in ("Personal", "Work"):
        workspace = create_workspace(session, user_id=user.id, name=name)
        repo = Repo.open(session, workspace.id)
        connection = repo.add_connection(
            client_name="claude-code",
            auth_mode=AuthMode.PAT,
            scopes=["memory:read", "memory:write"],
            writes_enabled=True,
            token_hash=TOKEN_HASH if name == "Personal" else None,
        )
        original = repo.add_memory(
            content=f"{name}: original",
            kind=MemoryKind.FACT,
            connection_id=connection.id,
            initiated_by=InitiatedBy.USER,
            embedding=[0.1] * EMBEDDING_DIM,
            agent_id="claude-code",
        )
        repo.supersede_memory(
            original.id,
            content=f"{name}: replacement",
            connection_id=connection.id,
            initiated_by=InitiatedBy.AGENT,
        )
        buried = repo.add_memory(
            content=f"{name}: deleted",
            kind=MemoryKind.DECISION,
            connection_id=connection.id,
            initiated_by=InitiatedBy.USER,
        )
        repo.tombstone_memory(buried.id)
        repo.put_skill(name="purse-save-policy", version="1.0.0", content="v1")
        repo.put_skill(name="purse-save-policy", version="1.1.0", content="v2")
        repo.add_api(
            name="stripe",
            provider="Stripe",
            base_url="https://api.stripe.com",
            auth_style="bearer",
            allowed_hosts=["api.stripe.com"],
            key_ciphertext=CIPHERTEXT,
            dek_wrapped=WRAPPED_DEK,
        )
        repo.record_audit(
            connection_id=connection.id,
            action="add_memory",
            target_type="memory",
            target_id=str(original.id),
        )
    session.flush()
    return user


def test_export_declares_its_format(session: Session, vault: User) -> None:
    payload = export_vault(session, vault.id)
    assert payload["format"] == EXPORT_FORMAT
    assert payload["format_version"] == EXPORT_FORMAT_VERSION
    assert payload["exported_at"]
    assert payload["user"]["email"] == vault.email


def test_export_is_plain_json_with_no_custom_encoder(session: Session, vault: User) -> None:
    """If any bytes ever reached the payload this would raise TypeError."""
    serialized = json.dumps(export_vault(session, vault.id))
    assert json.loads(serialized)["format"] == EXPORT_FORMAT


def test_export_covers_every_workspace(session: Session, vault: User) -> None:
    payload = export_vault(session, vault.id)
    assert sorted(w["name"] for w in payload["workspaces"]) == ["Personal", "Work"]


def test_export_carries_the_whole_memory_history(session: Session, vault: User) -> None:
    """Superseded and tombstoned rows included — the log *is* the data."""
    payload = export_vault(session, vault.id)
    personal = next(w for w in payload["workspaces"] if w["name"] == "Personal")
    contents = {m["content"] for m in personal["memories"]}
    assert contents == {
        "Personal: original",
        "Personal: replacement",
        "Personal: deleted",
    }

    by_content = {m["content"]: m for m in personal["memories"]}
    assert by_content["Personal: deleted"]["tombstone"] is True
    assert by_content["Personal: original"]["is_current"] is False
    assert by_content["Personal: replacement"]["is_current"] is True
    assert (
        by_content["Personal: replacement"]["supersedes"] == by_content["Personal: original"]["id"]
    )


def test_export_carries_memory_provenance(session: Session, vault: User) -> None:
    payload = export_vault(session, vault.id)
    personal = next(w for w in payload["workspaces"] if w["name"] == "Personal")
    original = next(m for m in personal["memories"] if m["content"] == "Personal: original")
    provenance = original["provenance"]
    assert provenance["initiated_by"] == "user"
    assert provenance["agent_id"] == "claude-code"
    assert provenance["connection_id"] in {c["id"] for c in personal["connections"]}


def test_export_carries_every_skill_version(session: Session, vault: User) -> None:
    payload = export_vault(session, vault.id)
    personal = next(w for w in payload["workspaces"] if w["name"] == "Personal")
    versions = {(s["name"], s["version"], s["is_head"]) for s in personal["skills"]}
    assert versions == {
        ("purse-save-policy", "1.0.0", False),
        ("purse-save-policy", "1.1.0", True),
    }


def test_export_omits_derived_embeddings(session: Session, vault: User) -> None:
    """Rebuildable from content, model-specific, and huge. Not user content."""
    payload = export_vault(session, vault.id)
    personal = next(w for w in payload["workspaces"] if w["name"] == "Personal")
    assert all("embedding" not in memory for memory in personal["memories"])


def test_export_lists_apis_by_name_only(session: Session, vault: User) -> None:
    payload = export_vault(session, vault.id)
    personal = next(w for w in payload["workspaces"] if w["name"] == "Personal")
    api = personal["apis"][0]
    assert api["name"] == "stripe"
    assert api["provider"] == "Stripe"
    assert api["base_url"] == "https://api.stripe.com"
    assert api["allowed_hosts"] == ["api.stripe.com"]
    assert api["key_exported"] is False
    assert "key_ciphertext" not in api
    assert "dek_wrapped" not in api


def test_no_key_material_survives_serialization(session: Session, vault: User) -> None:
    """The load-bearing test: grep the serialized bytes for the secrets.

    Checked raw, hex, and base64, because "we did not include the column" is a
    weaker claim than "the ciphertext does not appear in the output in any
    encoding a careless serializer might have chosen".
    """
    serialized = json.dumps(export_vault(session, vault.id))
    haystack = serialized.encode("utf-8")

    for secret in (CIPHERTEXT, WRAPPED_DEK):
        assert secret not in haystack
        assert secret.hex().encode() not in haystack
        assert base64.b64encode(secret) not in haystack
        assert secret.decode("ascii").encode() not in haystack


def test_no_token_hash_survives_serialization(session: Session, vault: User) -> None:
    serialized = json.dumps(export_vault(session, vault.id))
    assert TOKEN_HASH not in serialized
    connections = export_vault(session, vault.id)["workspaces"][0]["connections"]
    assert all("token_hash" not in c for c in connections)


def test_export_of_an_unknown_user_raises(session: Session) -> None:
    with pytest.raises(LookupError):
        export_vault(session, uuid.uuid4())
