"""Auth handling and error shape (C3.8) — no database.

Everything asserted here is true before a single row is read, so it is proved
without Postgres. That is not just convenience: these are the responses a
misconfigured client sees, and they must not depend on the database being up.

The shape itself is the contract. PRD §10 fixes ``{"error": {"code",
"message"}}`` for every failure, and C4 will re-use the same codes over MCP — so
a client that learned to read one error reads all of them.

Where a test needs to prove that authentication *succeeded* without touching
Postgres, it uses ``scoped_client`` and asserts a 403: a scope denial is
unreachable except by a request whose token already resolved to a caller.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from purse.gateway.rest import (
    AGENT_HEADER,
    MAX_AGENT_ID_LENGTH,
    GatewayContext,
    RequestContext,
    _agent_claim,
)
from tests.conftest import StubContext
from tests.gateway.conftest import GOOD_TOKEN

AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}

#: Every endpoint, as (method, path, json body). Parametrising over this is what
#: stops a sixth endpoint from being added with its own bespoke error shape.
ENDPOINTS = [
    ("POST", "/v1/memories", {"content": "hi", "kind": "fact", "initiated_by": "user"}),
    ("GET", "/v1/memories", None),
    ("GET", "/v1/memories/search?q=hi", None),
    ("PATCH", f"/v1/memories/{uuid.uuid4()}", {"content": "hi"}),
    ("DELETE", f"/v1/memories/{uuid.uuid4()}", None),
]


def _error(response: httpx2.Response) -> dict[str, Any]:
    """Assert the PRD §10 envelope and return the error object."""
    body = response.json()
    assert set(body) == {"error"}, body
    error: dict[str, Any] = body["error"]
    assert set(error) == {"code", "message"}, error
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    return error


# ---------------------------------------------------------------------------
# Bearer parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_every_endpoint_requires_a_bearer_token(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert _error(response)["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Basic abc123",
        "Token abc123",
        GOOD_TOKEN,  # the token, with no scheme at all
        "Bearer",
        "Bearer ",
        "Bearer    ",
    ],
)
def test_a_malformed_authorization_header_is_a_401_in_the_same_shape(
    client: TestClient, header: str
) -> None:
    response = client.get("/v1/memories", headers={"Authorization": header})
    assert response.status_code == 401
    assert _error(response)["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_the_bearer_scheme_is_case_insensitive(
    scoped_client: Callable[[set[str]], TestClient], scheme: str
) -> None:
    """RFC 7235 says the scheme is case-insensitive, and clients in the wild
    disagree about the spelling. Getting this wrong is one support ticket per
    client.

    A 403 rather than a 200 because the assertion is about *parsing*: reaching
    the scope check at all means the token was extracted and resolved.
    """
    client = scoped_client(set())
    response = client.get("/v1/memories", headers={"Authorization": f"{scheme} {GOOD_TOKEN}"})
    assert response.status_code == 403


def test_a_401_carries_a_www_authenticate_header(client: TestClient) -> None:
    """RFC 6750, and how some MCP clients decide to start an OAuth flow (C2)."""
    response = client.get("/v1/memories")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_rejected_token_is_a_401(client: TestClient) -> None:
    response = client.get("/v1/memories", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401
    assert _error(response)["code"] == "UNAUTHORIZED"


def test_the_401_message_does_not_say_why(client: TestClient) -> None:
    """Distinguishing unknown from revoked from expired is a probing oracle."""
    message = _error(client.get("/v1/memories", headers={"Authorization": "Bearer nope"}))[
        "message"
    ].lower()
    for leak in ("revoked", "expired", "unknown user", "workspace"):
        assert leak not in message


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_a_missing_write_scope_is_403_with_the_prd_code(
    scoped_client: Callable[[set[str]], TestClient],
) -> None:
    client = scoped_client({"memory:read"})
    response = client.post(
        "/v1/memories",
        headers=AUTH,
        json={"content": "hi", "kind": "fact", "initiated_by": "user"},
    )
    assert response.status_code == 403
    error = _error(response)
    assert error["code"] == "UNAUTHORIZED_SCOPE"
    assert "memory:write" in error["message"]


@pytest.mark.parametrize(
    ("method", "path", "body", "scope"),
    [
        ("POST", "/v1/memories", {"content": "hi"}, "memory:write"),
        ("GET", "/v1/memories", None, "memory:read"),
        ("GET", "/v1/memories/search?q=hi", None, "memory:read"),
        ("PATCH", f"/v1/memories/{uuid.uuid4()}", {"content": "hi"}, "memory:write"),
        ("DELETE", f"/v1/memories/{uuid.uuid4()}", None, "memory:write"),
    ],
)
def test_each_endpoint_demands_the_scope_prd_10_assigns_it(
    scoped_client: Callable[[set[str]], TestClient],
    method: str,
    path: str,
    body: dict[str, Any] | None,
    scope: str,
) -> None:
    client = scoped_client(set())
    response = client.request(method, path, json=body, headers=AUTH)
    assert response.status_code == 403
    assert scope in _error(response)["message"]


def test_scope_is_checked_before_the_body_is_validated_by_the_service(
    scoped_client: Callable[[set[str]], TestClient],
) -> None:
    """A caller without write scope must not learn whether its payload was valid."""
    client = scoped_client({"memory:read"})
    response = client.post("/v1/memories", headers=AUTH, json={"content": "", "kind": "nonsense"})
    assert response.status_code == 403


def test_the_default_require_scope_permits_everything(
    scoped_client: Callable[[set[str]], TestClient], client: TestClient
) -> None:
    """The M1 placeholder, asserted explicitly rather than left implicit.

    ``client`` is built without a ``require_scope`` argument, so a write with no
    scopes granted does *not* 403 — it gets as far as the service. That is the
    documented pre-C2 behaviour, and the reason the orchestrator must pass the
    real enforcer.
    """
    denied = scoped_client(set()).post("/v1/memories", headers=AUTH, json={"content": "hi"})
    assert denied.status_code == 403
    # Same request, default hook: fails validation instead of scope, i.e. it got
    # past the gate.
    permitted = client.post("/v1/memories", headers=AUTH, json={"content": ""})
    assert permitted.status_code == 422
    assert _error(permitted)["code"] == "VALIDATION"


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_a_missing_body_field_is_422_in_the_purse_shape_not_pydantics(
    client: TestClient,
) -> None:
    """FastAPI's default 422 body is ``{"detail": [...]}`` — a second shape a
    client would otherwise have to learn."""
    response = client.post("/v1/memories", headers=AUTH, json={"kind": "fact"})
    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "VALIDATION"
    assert "content" in error["message"]


def test_an_unknown_body_field_is_rejected(client: TestClient) -> None:
    """A typo'd ``initated_by`` that silently took the default would be a
    provenance error nobody ever notices."""
    response = client.post(
        "/v1/memories",
        headers=AUTH,
        json={"content": "hi", "kind": "fact", "initated_by": "user"},
    )
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION"


def test_a_non_uuid_path_parameter_is_422_not_500(client: TestClient) -> None:
    response = client.delete("/v1/memories/not-a-uuid", headers=AUTH)
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION"


@pytest.mark.parametrize("path", ["/v1/memories/search", "/v1/memories/search?q="])
def test_search_without_a_usable_query_is_422(client: TestClient, path: str) -> None:
    response = client.get(path, headers=AUTH)
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION"


def test_an_invalid_limit_is_422(client: TestClient) -> None:
    response = client.get("/v1/memories/search?q=hi&limit=0", headers=AUTH)
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION"


def test_a_forged_cursor_is_422_not_a_silent_first_page(client: TestClient) -> None:
    response = client.get("/v1/memories?cursor=obviously-not-a-cursor", headers=AUTH)
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION"


def test_the_search_route_is_not_shadowed_by_the_id_route(
    scoped_client: Callable[[set[str]], TestClient],
) -> None:
    """``/v1/memories/search`` must not be parsed as ``/v1/memories/{id}``.

    It cannot be, because ``search`` is not a uuid — but route order is what
    keeps that true, and route order is easy to disturb. A 403 (not a 422 about
    uuids) proves the search endpoint was the one matched.
    """
    client = scoped_client(set())
    response = client.get("/v1/memories/search?q=anything", headers=AUTH)
    assert response.status_code == 403
    assert "memory:read" in _error(response)["message"]


# ---------------------------------------------------------------------------
# The agent claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("claude-code/1.2.3", "claude-code/1.2.3"),
        ("  cursor/0.9  ", "cursor/0.9"),
        ("x" * 5000, "x" * MAX_AGENT_ID_LENGTH),
    ],
)
def test_the_agent_claim_is_normalised(header: str | None, expected: str | None) -> None:
    """Blank is not a claim, and a claim is not unbounded.

    ``agent_id`` is nullable precisely so "no claim" is representable; letting a
    blank header become ``""`` would put a value in the column that means the
    same as absent but does not compare equal to it.
    """
    scope = {
        "type": "http",
        "headers": [] if header is None else [(AGENT_HEADER.lower().encode(), header.encode())],
    }
    assert _agent_claim(Request(scope)) == expected


def test_the_request_context_delegates_the_trusted_half() -> None:
    """``RequestContext`` must not be able to invent a connection or a workspace —
    both come from the verified caller, only ``agent_id`` is the request's."""
    caller = StubContext(connection_id=uuid.uuid4(), workspace_id=uuid.uuid4())
    ctx = RequestContext(caller=caller, agent_id="pytest")

    assert ctx.connection_id == caller.connection_id
    assert ctx.workspace_id == caller.workspace_id
    assert ctx.agent_id == "pytest"


def test_an_auth_shaped_context_without_an_agent_id_is_a_valid_caller() -> None:
    """The C2 seam, asserted rather than assumed.

    ``purse.auth``'s ``AuthContext`` has ``connection_id``, ``workspace_id``,
    ``scopes`` (a ``frozenset[Scope]``) and ``writes_enabled`` — and no
    ``agent_id``, because a token does not identify an agent. This is that exact
    shape, standing in without importing the auth package.
    """

    @dataclass(frozen=True)
    class AuthContextShaped:
        connection_id: uuid.UUID
        workspace_id: uuid.UUID
        scopes: frozenset[str]
        writes_enabled: bool
        client_name: str

    caller: GatewayContext = AuthContextShaped(
        connection_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        scopes=frozenset({"memory:read", "memory:write"}),
        writes_enabled=True,
        client_name="claude-code",
    )
    # The gateway wraps it; the memory service takes the wrapper.
    ctx = RequestContext(caller=caller, agent_id=None)
    assert ctx.connection_id == caller.connection_id


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


def test_the_documented_surface_is_exactly_the_five_memory_endpoints(
    client: TestClient,
) -> None:
    """PRD §10 lists five memory tools. The REST smoke path mirrors them, and
    exposes nothing else."""
    schema = client.get("/openapi.json").json()
    routes = {
        (method.upper(), path) for path, methods in schema["paths"].items() for method in methods
    }
    assert routes == {
        ("POST", "/v1/memories"),
        ("GET", "/v1/memories"),
        ("GET", "/v1/memories/search"),
        ("PATCH", "/v1/memories/{memory_id}"),
        ("DELETE", "/v1/memories/{memory_id}"),
    }
