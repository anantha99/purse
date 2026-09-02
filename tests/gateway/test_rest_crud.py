"""The REST smoke path against a real database (C3.8).

This is the M1 acceptance test in miniature: HTTP in, canonical row out,
searchable from a second request. Real service, real Postgres, real audit rows —
only ``authenticate`` is faked, because C2 is being built in parallel.

``db``-marked, so it skips without a database (``tests/conftest.py``).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from purse.db.models import AuthMode
from purse.db.repo import Repo, create_user, create_workspace
from purse.gateway.rest import AGENT_HEADER, MAX_AGENT_ID_LENGTH, create_app
from purse.memory.engine import NullEngine
from tests.conftest import RaisingEngine, StubContext
from tests.gateway.conftest import GOOD_TOKEN, DbGateway, fake_authenticate

pytestmark = pytest.mark.db

AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_add_then_search_round_trips(db_client: TestClient) -> None:
    """PRD §15 M1: "REST add/search". If this fails, the spine is not connected."""
    created = db_client.post(
        "/v1/memories",
        headers=AUTH,
        json={"content": "I prefer TypeScript.", "kind": "preference", "initiated_by": "user"},
    )
    assert created.status_code == 201
    body = created.json()
    assert uuid.UUID(body["id"])
    assert body["content"] == "I prefer TypeScript."
    assert body["kind"] == "preference"
    assert body["supersedes"] is None
    assert body["created_at"]

    found = db_client.get("/v1/memories/search?q=typescript", headers=AUTH)
    assert found.status_code == 200
    results = found.json()["results"]
    assert [hit["id"] for hit in results] == [body["id"]]
    assert results[0]["content"] == "I prefer TypeScript."


def test_a_created_memory_carries_provenance(db_client: TestClient, db_gateway: DbGateway) -> None:
    body = db_client.post(
        "/v1/memories",
        headers={**AUTH, AGENT_HEADER: "claude-code/1.2.3"},
        json={"content": "Ship Fridays.", "kind": "decision"},
    ).json()

    provenance = body["provenance"]
    # Trusted: proved by the token.
    assert provenance["connection_id"] == str(db_gateway.ctx.connection_id)
    # Claimed: whatever the caller put in the header, recorded and no more.
    assert provenance["agent_id"] == "claude-code/1.2.3"
    # The default when the caller does not say otherwise: an agent is writing.
    assert provenance["initiated_by"] == "agent"


def test_agent_id_is_null_when_the_caller_does_not_claim_one(db_client: TestClient) -> None:
    """The header is optional — ``agent_id`` is nullable in the schema for exactly
    this reason, and a missing claim must not become the empty string."""
    body = db_client.post("/v1/memories", headers=AUTH, json={"content": "anonymous"}).json()
    assert body["provenance"]["agent_id"] is None


def test_an_over_long_agent_claim_is_truncated_not_rejected(db_client: TestClient) -> None:
    """A courtesy label must not be able to fail an otherwise valid write, nor to
    write unbounded text into an audit row."""
    response = db_client.post(
        "/v1/memories",
        headers={**AUTH, AGENT_HEADER: "a" * 5000},
        json={"content": "verbose client"},
    )
    assert response.status_code == 201
    assert response.json()["provenance"]["agent_id"] == "a" * MAX_AGENT_ID_LENGTH


def test_the_agent_claim_reaches_the_audit_log(
    db_client: TestClient, db_gateway: DbGateway
) -> None:
    db_client.post(
        "/v1/memories", headers={**AUTH, AGENT_HEADER: "cursor/0.9"}, json={"content": "hi"}
    )
    with db_gateway.session_factory() as session:
        entries = Repo.open(session, db_gateway.ctx.workspace_id).list_audit()
    assert [entry.agent_id for entry in entries] == ["cursor/0.9"]


def test_the_response_never_carries_an_embedding(db_client: TestClient) -> None:
    """Derived, droppable, and nobody's business but the engine's."""
    body = db_client.post("/v1/memories", headers=AUTH, json={"content": "hello"}).json()
    assert "embedding" not in body
    assert "workspace_id" not in body


# ---------------------------------------------------------------------------
# Validation, over HTTP
# ---------------------------------------------------------------------------


def test_oversized_content_is_413(db_client: TestClient) -> None:
    from purse.memory.service import MAX_CONTENT_BYTES

    response = db_client.post(
        "/v1/memories", headers=AUTH, json={"content": "a" * (MAX_CONTENT_BYTES + 1)}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_an_unknown_kind_is_422(db_client: TestClient) -> None:
    response = db_client.post(
        "/v1/memories", headers=AUTH, json={"content": "hi", "kind": "profile"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION"


def test_a_rejected_write_leaves_no_row_behind(db_client: TestClient) -> None:
    """The dependency rolls back on any exception — including one raised from a
    handler-mapped error, which still unwinds through the session."""
    db_client.post("/v1/memories", headers=AUTH, json={"content": "hi", "kind": "profile"})
    listed = db_client.get("/v1/memories", headers=AUTH).json()
    assert listed["items"] == []


# ---------------------------------------------------------------------------
# Update / delete
# ---------------------------------------------------------------------------


def test_patch_supersedes_and_returns_the_new_id(db_client: TestClient) -> None:
    original = db_client.post(
        "/v1/memories", headers=AUTH, json={"content": "I prefer Python."}
    ).json()

    updated = db_client.patch(
        f"/v1/memories/{original['id']}", headers=AUTH, json={"content": "I prefer TypeScript."}
    )
    assert updated.status_code == 200
    new = updated.json()
    assert new["id"] != original["id"]
    assert new["supersedes"] == original["id"]

    listed = db_client.get("/v1/memories", headers=AUTH).json()["items"]
    assert [item["id"] for item in listed] == [new["id"]]


def test_patching_an_unknown_id_is_404(db_client: TestClient) -> None:
    response = db_client.patch(
        f"/v1/memories/{uuid.uuid4()}", headers=AUTH, json={"content": "nope"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_patching_a_superseded_id_is_404(db_client: TestClient) -> None:
    original = db_client.post("/v1/memories", headers=AUTH, json={"content": "v1"}).json()
    db_client.patch(f"/v1/memories/{original['id']}", headers=AUTH, json={"content": "v2"})

    again = db_client.patch(
        f"/v1/memories/{original['id']}", headers=AUTH, json={"content": "v2 again"}
    )
    assert again.status_code == 404


def test_delete_tombstones_and_is_idempotent(db_client: TestClient) -> None:
    created = db_client.post("/v1/memories", headers=AUTH, json={"content": "forget me"}).json()

    first = db_client.delete(f"/v1/memories/{created['id']}", headers=AUTH)
    assert first.status_code == 200
    assert first.json() == {"id": created["id"], "deleted": True}

    second = db_client.delete(f"/v1/memories/{created['id']}", headers=AUTH)
    assert second.status_code == 200, "a retried delete must not become a 404"

    assert db_client.get("/v1/memories", headers=AUTH).json()["items"] == []
    assert db_client.get("/v1/memories/search?q=forget", headers=AUTH).json()["results"] == []


def test_deleting_an_unknown_id_is_404(db_client: TestClient) -> None:
    response = db_client.delete(f"/v1/memories/{uuid.uuid4()}", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_pages_with_a_cursor(db_client: TestClient) -> None:
    written = {
        db_client.post("/v1/memories", headers=AUTH, json={"content": f"memory {index}"}).json()[
            "id"
        ]
        for index in range(5)
    }

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = "/v1/memories?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = db_client.get(query, headers=AUTH).json()
        seen.extend(item["id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert pages == 3
    assert len(seen) == len(set(seen)), "a memory was returned on two pages"
    assert set(seen) == written


def test_an_empty_workspace_lists_nothing(db_client: TestClient) -> None:
    body = db_client.get("/v1/memories", headers=AUTH).json()
    assert body == {"items": [], "next_cursor": None}


# ---------------------------------------------------------------------------
# Audit and durability
# ---------------------------------------------------------------------------


def test_the_full_crud_cycle_is_audited(db_client: TestClient, db_gateway: DbGateway) -> None:
    created = db_client.post("/v1/memories", headers=AUTH, json={"content": "v1"}).json()
    updated = db_client.patch(
        f"/v1/memories/{created['id']}", headers=AUTH, json={"content": "v2"}
    ).json()
    db_client.delete(f"/v1/memories/{updated['id']}", headers=AUTH)

    # Read the audit log through the same factory the app used, so this sees the
    # rows the requests committed.
    with db_gateway.session_factory() as session:
        entries = Repo.open(session, db_gateway.ctx.workspace_id).list_audit()

    assert [entry.action for entry in entries] == [
        "memory.delete",
        "memory.update",
        "memory.add",
    ]
    assert all(entry.connection_id == db_gateway.ctx.connection_id for entry in entries)


def test_an_exploding_engine_still_returns_201_and_keeps_the_write(
    db_gateway: DbGateway,
) -> None:
    """C3.3 over HTTP: Mem0 being down must not surface as a failed request."""
    with db_gateway.client(engine=RaisingEngine()) as client:
        created = client.post("/v1/memories", headers=AUTH, json={"content": "index is on fire"})
        assert created.status_code == 201

        found = client.get("/v1/memories/search?q=on fire", headers=AUTH).json()
        assert [hit["id"] for hit in found["results"]] == [created.json()["id"]]


def test_writes_persist_across_requests(db_gateway: DbGateway) -> None:
    """Two separate clients, two separate request-scoped sessions.

    Proves the per-request commit is real rather than an artefact of one session
    happening to hold the row in its identity map.
    """
    with db_gateway.client() as writer:
        created = writer.post("/v1/memories", headers=AUTH, json={"content": "durable"}).json()

    with db_gateway.client() as reader:
        listed = reader.get("/v1/memories", headers=AUTH).json()["items"]

    assert [item["id"] for item in listed] == [created["id"]]


def test_search_does_not_leak_across_workspaces(db_gateway: DbGateway) -> None:
    """The gateway is workspace-scoped by the authenticated connection (PRD §10)."""
    make_session = db_gateway.session_factory

    with make_session() as setup:
        other_user = create_user(setup, email=f"other-{uuid.uuid4().hex[:8]}@example.test")
        other_ws = create_workspace(setup, user_id=other_user.id, name="Theirs")
        other_conn = Repo.open(setup, other_ws.id).add_connection(
            client_name="cursor", auth_mode=AuthMode.PAT, writes_enabled=True
        )
        other_ctx = StubContext(connection_id=other_conn.id, workspace_id=other_ws.id)
        setup.commit()

    other_app = create_app(
        session_factory=make_session,
        engine=NullEngine(),
        authenticate=fake_authenticate(other_ctx),
    )
    with TestClient(other_app) as theirs:
        theirs.post("/v1/memories", headers=AUTH, json={"content": "their secret needle"})

    with db_gateway.client() as ours:
        assert ours.get("/v1/memories/search?q=needle", headers=AUTH).json()["results"] == []
        assert ours.get("/v1/memories", headers=AUTH).json()["items"] == []


def test_session_type_is_not_leaked_into_responses(db_client: TestClient) -> None:
    """A detached-instance bug would show up as a 500 here, not a wrong value."""
    created = db_client.post("/v1/memories", headers=AUTH, json={"content": "hello"})
    assert created.status_code == 201
    assert set(created.json()) == {
        "id",
        "content",
        "kind",
        "created_at",
        "supersedes",
        "provenance",
    }
