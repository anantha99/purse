"""The ``/web`` surface coexists on the one assembled app (no database).

Proves the mount topology: ``/web/*`` is claimed by the dashboard app (not
swallowed by the empty-prefix REST catch-all), and an unconfigured instance
mounts cleanly — ``/web/login`` answers rather than crashing boot. None of these
touch Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from purse.gateway.asgi import create_purse_app

DUMMY_DB_URL = "postgresql+psycopg://u:p@127.0.0.1:1/none"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_purse_app(
        public_url="https://vault.example.test",
        secret="integration-test-signing-secret",  # noqa: S106 - test fixture, not a credential
        database_url=DUMMY_DB_URL,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_web_login_is_mounted_and_unconfigured_by_default(client: TestClient) -> None:
    # No PURSE_OWNER_PASSWORD in the env → login disabled, but the route exists
    # and the app booted. (503, not a 404 from the REST catch-all.)
    response = client.post("/web/login", json={"password": "x"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LOGIN_DISABLED"


def test_web_protected_route_guards_auth(client: TestClient) -> None:
    response = client.get("/web/memories")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_v1_surface_still_resolves_alongside_web(client: TestClient) -> None:
    # The /web mount must not shadow /v1 (both fall after the MCP/OAuth routes).
    response = client.get("/v1/memories")
    assert response.status_code == 401
    assert response.json()["error"]["code"]
