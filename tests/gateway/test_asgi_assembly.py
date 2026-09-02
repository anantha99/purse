"""The assembled Purse app: route topology, without a database (M2 integration).

These prove the three surfaces coexist on one app and — the load-bearing one —
that OAuth discovery lands at the ROOT, not under /mcp, so a client's well-known
probe resolves. None of these paths query Postgres, so they run everywhere; the
full authenticated round trip against real Postgres is the db-marked suites in
test_mcp_db.py and test_oauth_provider_db.py, exercised in CI.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from purse.gateway.asgi import create_purse_app

# A syntactically valid URL whose engine never connects — every assertion here
# hits a route that resolves before any DB query.
DUMMY_DB_URL = "postgresql+psycopg://u:p@127.0.0.1:1/none"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_purse_app(
        public_url="https://vault.example.test",
        secret="integration-test-signing-secret",  # noqa: S106 - test fixture, not a credential
        database_url=DUMMY_DB_URL,
    )
    # `with` runs the FastMCP app's lifespan (the stateless session manager).
    with TestClient(app) as test_client:
        yield test_client


def test_oauth_discovery_is_at_the_root(client: TestClient) -> None:
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    # The CIMD anti-downgrade gate: both must be present or Anthropic silently
    # falls back to deprecated DCR (spike finding).
    assert metadata["client_id_metadata_document_supported"] is True
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    # Issuer reflects the public URL, not localhost.
    assert metadata["issuer"].rstrip("/") == "https://vault.example.test"


def test_rest_surface_is_mounted_and_guards_auth(client: TestClient) -> None:
    # No bearer -> 401 in the structured shape, and no DB was touched to decide it.
    response = client.get("/v1/memories")
    assert response.status_code == 401
    assert response.json()["error"]["code"]


def test_unknown_path_is_a_clean_404(client: TestClient) -> None:
    assert client.get("/not/a/route").status_code == 404
