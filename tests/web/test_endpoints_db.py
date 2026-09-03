"""Full-flow ``/web`` tests against real Postgres (C7).

Skipped locally (no database), run in CI (``REQUIRE_DB=1``). Each drives the
mounted app through a genuine session: login → token → the operator's
workspace-scoped view, exactly as the Next.js BFF will.
"""

from __future__ import annotations

import pytest

from tests.web.conftest import PASSWORD, WebVault

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_login_returns_operator_and_workspace(web_vault: WebVault) -> None:
    response = web_vault.client.post("/login", json={"password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"].endswith("@example.test")
    assert body["workspace"]["id"] == str(web_vault.workspace_id)
    assert body["workspace"]["name"] == "Personal"
    assert body["session_token"]


def test_login_with_wrong_password_is_invalid_credentials(web_vault: WebVault) -> None:
    response = web_vault.client.post("/login", json={"password": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_session_echoes_the_operator(web_vault: WebVault) -> None:
    response = web_vault.client.get("/session", headers=web_vault.auth())
    assert response.status_code == 200
    body = response.json()
    assert body["workspace"]["id"] == str(web_vault.workspace_id)
    assert body["writes_enabled_default"] is False


def test_unauthenticated_request_is_401(web_vault: WebVault) -> None:
    assert web_vault.client.get("/memories").status_code == 401
    assert web_vault.client.get("/audit").status_code == 401
    assert web_vault.client.get("/workspace").status_code == 401


def test_logout_is_204(web_vault: WebVault) -> None:
    assert web_vault.client.post("/logout").status_code == 204


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


def test_add_then_list_memories(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    added = web_vault.client.post(
        "/memories",
        headers=auth,
        json={"content": "I prefer TypeScript", "kind": "preference"},
    )
    assert added.status_code == 201, added.text
    record = added.json()
    assert record["content"] == "I prefer TypeScript"
    assert record["provenance"]["initiated_by"] == "user"
    # Operator writes are attributed to the dashboard connection, resolved by name.
    assert record["provenance"]["client_name"] == "Purse Web (operator)"
    assert record["superseded_count"] == 0

    listing = web_vault.client.get("/memories", headers=auth)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert any(item["id"] == record["id"] for item in items)
    assert all("superseded_count" in item for item in items)
    assert all("client_name" in item["provenance"] for item in items)


def test_supersede_records_history_and_counts(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    first = web_vault.client.post(
        "/memories", headers=auth, json={"content": "v1", "kind": "fact"}
    ).json()

    edited = web_vault.client.patch(
        f"/memories/{first['id']}", headers=auth, json={"content": "v2"}
    )
    assert edited.status_code == 200
    new_id = edited.json()["id"]
    assert new_id != first["id"]
    assert edited.json()["superseded_count"] == 1

    history = web_vault.client.get(f"/memories/{new_id}/history", headers=auth)
    assert history.status_code == 200
    versions = history.json()["versions"]
    assert [v["content"] for v in versions] == ["v1", "v2"]  # oldest -> newest

    # The superseded original is no longer in the current view.
    listing = web_vault.client.get("/memories", headers=auth).json()["items"]
    assert all(item["id"] != first["id"] for item in listing)


def test_delete_tombstones_a_memory(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    created = web_vault.client.post(
        "/memories", headers=auth, json={"content": "forget me", "kind": "fact"}
    ).json()

    deleted = web_vault.client.delete(f"/memories/{created['id']}", headers=auth)
    assert deleted.status_code == 200
    assert deleted.json() == {"id": created["id"], "deleted": True}

    listing = web_vault.client.get("/memories", headers=auth).json()["items"]
    assert all(item["id"] != created["id"] for item in listing)


def test_search_returns_scored_items(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    web_vault.client.post(
        "/memories", headers=auth, json={"content": "deploys run on Fridays", "kind": "decision"}
    )
    results = web_vault.client.get("/memories/search", headers=auth, params={"q": "Fridays"})
    assert results.status_code == 200
    hits = results.json()["results"]
    assert hits
    assert "score" in hits[0]
    assert "superseded_count" in hits[0]
    assert hits[0]["provenance"]["client_name"] == "Purse Web (operator)"


def test_list_filters_by_kind(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    web_vault.client.post("/memories", headers=auth, json={"content": "a", "kind": "fact"})
    web_vault.client.post("/memories", headers=auth, json={"content": "b", "kind": "preference"})

    only_pref = web_vault.client.get("/memories", headers=auth, params={"kind": "preference"})
    assert only_pref.status_code == 200
    kinds = {item["kind"] for item in only_pref.json()["items"]}
    assert kinds == {"preference"}


def test_a_session_sees_only_the_operator_workspace(
    web_vault: WebVault, second_workspace: object
) -> None:
    # A memory exists in another workspace (see the `second_workspace` fixture);
    # the operator's session must not surface it.
    listing = web_vault.client.get("/memories", headers=web_vault.auth()).json()["items"]
    assert all("another vault" not in item["content"] for item in listing)

    search = web_vault.client.get(
        "/memories/search", headers=web_vault.auth(), params={"q": "another vault"}
    ).json()["results"]
    assert search == []


# ---------------------------------------------------------------------------
# Connections + tokens
# ---------------------------------------------------------------------------


def test_mint_pat_returns_token_once_and_creates_a_connection(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    minted = web_vault.client.post(
        "/tokens",
        headers=auth,
        json={"client_name": "Codex", "scopes": ["memory:read"], "writes_enabled": False},
    )
    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["token"].startswith("purse_pat_")
    assert body["connection"]["client_name"] == "Codex"
    assert body["connection"]["scopes"] == ["memory:read"]

    connections = web_vault.client.get("/connections", headers=auth).json()["connections"]
    assert any(c["id"] == body["connection"]["id"] for c in connections)


def test_revoke_connection_is_idempotent(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    conn_id = web_vault.client.post(
        "/tokens", headers=auth, json={"client_name": "temp", "scopes": [], "writes_enabled": False}
    ).json()["connection"]["id"]

    first = web_vault.client.delete(f"/connections/{conn_id}", headers=auth)
    assert first.status_code == 200
    assert first.json() == {"id": conn_id, "revoked": True}
    # Idempotent: revoking again still succeeds.
    again = web_vault.client.delete(f"/connections/{conn_id}", headers=auth)
    assert again.status_code == 200

    listed = web_vault.client.get("/connections", headers=auth).json()["connections"]
    revoked = next(c for c in listed if c["id"] == conn_id)
    assert revoked["revoked_at"] is not None


def test_revoke_unknown_connection_is_404(web_vault: WebVault) -> None:
    import uuid

    response = web_vault.client.delete(f"/connections/{uuid.uuid4()}", headers=web_vault.auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_mint_pat_rejects_unknown_scope(web_vault: WebVault) -> None:
    response = web_vault.client.post(
        "/tokens",
        headers=web_vault.auth(),
        json={"client_name": "bad", "scopes": ["memory:teleport"], "writes_enabled": False},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION"


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


SKILL_DOC = """---
name: save-policy
description: What to remember
version: 1.0.0
---

Save durable facts.
"""


def test_upsert_then_get_skill_with_versions(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    put = web_vault.client.put("/skills/save-policy", headers=auth, json={"content": SKILL_DOC})
    assert put.status_code == 200, put.text
    assert put.json() == {"name": "save-policy", "version": "1.0.0"}

    got = web_vault.client.get("/skills/save-policy", headers=auth)
    assert got.status_code == 200
    body = got.json()
    assert body["name"] == "save-policy"
    assert body["version"] == "1.0.0"
    assert "Save durable facts." in body["body"]
    assert any(v["version"] == "1.0.0" for v in body["versions"])

    listing = web_vault.client.get("/skills", headers=auth).json()["skills"]
    assert any(s["name"] == "save-policy" for s in listing)


# ---------------------------------------------------------------------------
# Audit / export / workspace
# ---------------------------------------------------------------------------


def test_audit_is_newest_first(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    web_vault.client.post("/memories", headers=auth, json={"content": "one", "kind": "fact"})
    web_vault.client.post("/memories", headers=auth, json={"content": "two", "kind": "fact"})

    entries = web_vault.client.get("/audit", headers=auth).json()["entries"]
    actions = [e["action"] for e in entries]
    assert actions[:2] == ["memory.add", "memory.add"]
    assert all(e["client_name"] == "Purse Web (operator)" for e in entries)
    # Newest-first: created_at is non-increasing down the list.
    timestamps = [e["created_at"] for e in entries]
    assert timestamps == sorted(timestamps, reverse=True)


def test_export_returns_the_documented_json_as_an_attachment(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    web_vault.client.post("/memories", headers=auth, json={"content": "keep me", "kind": "fact"})

    response = web_vault.client.get("/export", headers=auth)
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    payload = response.json()
    assert payload["format"] == "purse.vault.export"
    assert payload["workspaces"]
    contents = [m["content"] for ws in payload["workspaces"] for m in ws["memories"]]
    assert "keep me" in contents


def test_workspace_counts(web_vault: WebVault) -> None:
    auth = web_vault.auth()
    web_vault.client.post("/memories", headers=auth, json={"content": "x", "kind": "fact"})

    counts = web_vault.client.get("/workspace", headers=auth).json()
    assert counts["memories"] >= 1
    assert set(counts) == {"memories", "skills", "apis", "connections"}
