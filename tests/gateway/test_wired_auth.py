"""End-to-end M1 spine: real PAT auth wired into the real REST app (db-marked).

Everything else in tests/gateway uses a fake ``authenticate``; this file is the
one place the whole stack runs together — ``mint_pat`` → bearer request →
scope check → canonical insert → audit row — the way a self-hoster's curl
session will exercise it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from purse.auth.provisioning import mint_pat, revoke_connection
from purse.auth.scopes import Scope
from purse.db.repo import Repo, create_user, create_workspace
from purse.gateway.app import create_default_app

pytestmark = pytest.mark.db


@dataclass
class WiredVault:
    workspace_id: uuid.UUID
    make_session: object

    def client(self) -> TestClient:
        return TestClient(
            create_default_app(make_session=self.make_session)  # type: ignore[arg-type]
        )

    def token(
        self, *, scopes: Iterator[Scope] | list[Scope], writes_enabled: bool, revoke: bool = False
    ) -> str:
        with self.make_session() as session:  # type: ignore[operator]
            connection, raw = mint_pat(
                session,
                workspace_id=self.workspace_id,
                client_name="wired-test",
                scopes=list(scopes),
                writes_enabled=writes_enabled,
            )
            if revoke:
                revoke_connection(session, connection.id)
            session.commit()
            return raw.reveal()


@pytest.fixture
def vault(migrated_engine: Engine) -> Iterator[WiredVault]:
    connection = migrated_engine.connect()
    transaction = connection.begin()

    def make_session() -> Session:
        return Session(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    with make_session() as setup:
        user = create_user(setup, email=f"wired-{uuid.uuid4().hex[:10]}@example.test")
        workspace = create_workspace(setup, user_id=user.id, name="Personal")
        setup.commit()

    try:
        yield WiredVault(workspace_id=workspace.id, make_session=make_session)
    finally:
        transaction.rollback()
        connection.close()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_m1_spine_end_to_end(vault: WiredVault) -> None:
    token = vault.token(scopes=[Scope.MEMORY_READ, Scope.MEMORY_WRITE], writes_enabled=True)
    with vault.client() as client:
        created = client.post(
            "/v1/memories",
            headers=bearer(token),
            json={"content": "I prefer TypeScript.", "kind": "preference", "initiated_by": "user"},
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]

        found = client.get("/v1/memories/search?q=typescript", headers=bearer(token))
        assert found.status_code == 200
        assert [hit["id"] for hit in found.json()["results"]] == [memory_id]

    with vault.make_session() as session:  # type: ignore[operator]
        actions = [entry.action for entry in Repo.open(session, vault.workspace_id).list_audit()]
    assert "memory.add" in actions


def test_read_only_pat_cannot_write_but_can_read(vault: WiredVault) -> None:
    token = vault.token(scopes=[Scope.MEMORY_READ], writes_enabled=True)
    with vault.client() as client:
        denied = client.post("/v1/memories", headers=bearer(token), json={"content": "nope"})
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "UNAUTHORIZED_SCOPE"

        listed = client.get("/v1/memories", headers=bearer(token))
        assert listed.status_code == 200


def test_writes_disabled_blocks_even_a_granted_write_scope(vault: WiredVault) -> None:
    token = vault.token(scopes=[Scope.MEMORY_READ, Scope.MEMORY_WRITE], writes_enabled=False)
    with vault.client() as client:
        denied = client.post("/v1/memories", headers=bearer(token), json={"content": "nope"})
        assert denied.status_code == 403


def test_revoked_and_garbage_tokens_are_the_same_401(vault: WiredVault) -> None:
    revoked = vault.token(
        scopes=[Scope.MEMORY_READ, Scope.MEMORY_WRITE], writes_enabled=True, revoke=True
    )
    with vault.client() as client:
        revoked_response = client.get("/v1/memories", headers=bearer(revoked))
        garbage_response = client.get("/v1/memories", headers=bearer("purse_pat_" + "x" * 43))
        assert revoked_response.status_code == garbage_response.status_code == 401
        # No oracle: a revoked token and an unknown token are indistinguishable.
        assert revoked_response.json() == garbage_response.json()
