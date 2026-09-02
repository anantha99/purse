"""Rate limiting enforced at the two write surfaces, and shared across them (C2.10).

The limiter's arithmetic is proved in ``test_ratelimit.py``; here it is proved
*wired*: a burst of writes over MCP returns ``RATE_LIMITED`` past the budget, the
same over REST returns 429 in the PRD §10 envelope, reads are never charged, and
one connection hitting both surfaces through a single limiter instance draws on
one budget (exactly how :mod:`purse.gateway.asgi` wires it).

The write paths reach the service, so those tests are db-marked (skipped locally,
run in CI). The "reads are never charged" no-DB test needs no row: it proves the
limiter is not even *consulted* for a read.

Everything uses a tiny injected limit and a frozen clock — the burst is all a
connection gets, and no real time passes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import anyio
import mcp.types as mcp_types
import pytest
from fastmcp.utilities.tests import asgi_client
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from purse.gateway.mcp import MCPErrorCode
from purse.gateway.ratelimit import WRITES_BUCKET, Limit, RateLimiter
from purse.gateway.rest import create_app
from purse.memory.engine import NullEngine
from tests.conftest import StubContext
from tests.gateway.conftest import (
    GOOD_TOKEN,
    MCP_GOOD_TOKEN,
    DbGateway,
    MCPDbVault,
    fake_authenticate,
)

AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}
WRITE_BODY = {"content": "x", "kind": "fact", "initiated_by": "agent"}


def _tiny_limiter(capacity: int = 2) -> RateLimiter:
    """A limiter with a small write budget and a frozen clock (no refill).

    A clock that never advances means the burst is all a connection gets — the
    cleanest way to prove the *cap* without also re-testing refill.
    """
    return RateLimiter(
        {WRITES_BUCKET: Limit(capacity=capacity, per_seconds=60.0)},
        now=lambda: 0.0,
        idle_eviction_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# Reads are never limited (no DB — the limiter is not even consulted)
# ---------------------------------------------------------------------------


class RecordingLimiter(RateLimiter):
    """A limiter that records every ``check`` so a test can prove a read made none."""

    def __init__(self) -> None:
        super().__init__(now=lambda: 0.0)
        self.calls: list[uuid.UUID] = []

    def check(
        self, connection_id: uuid.UUID, *, bucket: str = WRITES_BUCKET, cost: int = 1
    ) -> None:
        self.calls.append(connection_id)
        super().check(connection_id, bucket=bucket, cost=cost)


def test_a_read_never_consults_the_limiter(fake_ctx: StubContext) -> None:
    """No row needed: a GET must not call the limiter at all (reads are cheap).

    The read reaches an unbound session and 500s at the service — irrelevant here;
    what matters is that ``check`` was never invoked, so no read can ever exhaust
    a write budget.
    """
    limiter = RecordingLimiter()
    app = create_app(
        session_factory=Session,
        engine=NullEngine(),
        authenticate=fake_authenticate(fake_ctx),
        limiter=limiter,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/v1/memories", headers=AUTH)
        client.get("/v1/memories/search?q=x", headers=AUTH)

    assert limiter.calls == []


# ---------------------------------------------------------------------------
# MCP surface (db-marked)
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_mcp_writes_are_limited_and_reads_are_not(mcp_db: MCPDbVault) -> None:
    server = mcp_db.build_with_limiter(_tiny_limiter(capacity=2))

    async def body() -> None:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            first = await client.call_tool_mcp("add_memory", WRITE_BODY)
            assert not first.is_error, _text(first)
            second = await client.call_tool_mcp("add_memory", WRITE_BODY)
            assert not second.is_error, _text(second)

            # Third write is over budget -> the RATE_LIMITED envelope.
            third = await client.call_tool_mcp("add_memory", WRITE_BODY)
            assert _error(third)["code"] == MCPErrorCode.RATE_LIMITED

            # Reads stay free even with the write budget spent.
            for _ in range(5):
                read = await client.call_tool_mcp("search_memory", {"query": "x"})
                assert not read.is_error, _text(read)
                listed = await client.call_tool_mcp("list_memories", {})
                assert not listed.is_error, _text(listed)

    anyio.run(body)


# ---------------------------------------------------------------------------
# REST surface (db-marked)
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_rest_writes_over_budget_return_429_in_the_prd_shape(db_gateway: DbGateway) -> None:
    client = db_gateway.client_with_limiter(_tiny_limiter(capacity=2))

    assert client.post("/v1/memories", headers=AUTH, json=WRITE_BODY).status_code == 201
    assert client.post("/v1/memories", headers=AUTH, json=WRITE_BODY).status_code == 201

    refused = client.post("/v1/memories", headers=AUTH, json=WRITE_BODY)
    assert refused.status_code == 429
    error = refused.json()["error"]
    assert set(error) == {"code", "message"}
    assert error["code"] == "RATE_LIMITED"
    # RFC 6585: a 429 should say how long to wait.
    assert int(refused.headers["Retry-After"]) >= 1


@pytest.mark.db
def test_rest_reads_are_not_charged_against_the_write_budget(db_gateway: DbGateway) -> None:
    client = db_gateway.client_with_limiter(_tiny_limiter(capacity=1))

    assert client.post("/v1/memories", headers=AUTH, json=WRITE_BODY).status_code == 201
    # Budget spent; reads must still succeed, and must not refill it.
    for _ in range(5):
        assert client.get("/v1/memories", headers=AUTH).status_code == 200
        assert client.get("/v1/memories/search?q=x", headers=AUTH).status_code == 200
    assert client.post("/v1/memories", headers=AUTH, json=WRITE_BODY).status_code == 429


# ---------------------------------------------------------------------------
# One connection, one budget, across both surfaces
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_one_connection_shares_a_single_budget_across_mcp_and_rest(mcp_db: MCPDbVault) -> None:
    """Mirrors the asgi wiring: the SAME limiter instance backs both surfaces, so
    a capacity-2 budget spent one-over-REST and one-over-MCP refuses the third on
    either surface."""
    limiter = _tiny_limiter(capacity=2)

    # Both apps bound to the same connection, workspace, sessions, and limiter —
    # exactly what create_purse_app assembles, minus the real auth providers.
    mcp_server = mcp_db.build_with_limiter(limiter)
    caller = StubContext(connection_id=mcp_db.connection_id, workspace_id=mcp_db.workspace_id)
    rest_app = create_app(
        session_factory=mcp_db.session_factory,
        engine=NullEngine(),
        authenticate=fake_authenticate(caller),
        limiter=limiter,
    )

    with TestClient(rest_app) as rest:
        # Write #1 over REST.
        assert rest.post("/v1/memories", headers=AUTH, json=WRITE_BODY).status_code == 201

        # Write #2 over MCP, same connection.
        async def mcp_write() -> mcp_types.CallToolResult:
            async with asgi_client(mcp_server, auth=MCP_GOOD_TOKEN) as client:
                return await client.call_tool_mcp("add_memory", WRITE_BODY)

        second = anyio.run(mcp_write)
        assert not second.is_error, _text(second)

        # Budget of 2 is now spent: the third write is refused on BOTH surfaces.
        third_rest = rest.post("/v1/memories", headers=AUTH, json=WRITE_BODY)
        assert third_rest.status_code == 429
        assert third_rest.json()["error"]["code"] == "RATE_LIMITED"

        third_mcp = anyio.run(mcp_write)
        assert _error(third_mcp)["code"] == MCPErrorCode.RATE_LIMITED


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text(result: mcp_types.CallToolResult) -> str:
    if result.content and isinstance(result.content[0], mcp_types.TextContent):
        return result.content[0].text
    return "<no content>"


def _error(result: mcp_types.CallToolResult) -> dict[str, Any]:
    assert result.is_error
    assert isinstance(result.content[0], mcp_types.TextContent)
    return cast(dict[str, Any], json.loads(result.content[0].text)["error"])
