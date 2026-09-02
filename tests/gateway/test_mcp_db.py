"""MCP tools end to end against real Postgres (C4.6, C4.7) — db-marked.

The full round trip through the five tools, plus the two properties that only a
real database proves: provenance is the *token's* connection (never a client
claim, PRD §10 / C4.7), and ``writes_enabled=False`` cuts a write at the gateway
before a row is ever written.

Skipped locally (no Postgres); run in CI against pgvector pg17, where
``REQUIRE_DB=1`` turns a skip into a failure. The savepoint-per-commit fixture
(``mcp_db``) lets each tool call commit as it does in production while the whole
test still rolls back.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import anyio
import mcp.types as mcp_types
import pytest
from fastmcp.utilities.tests import asgi_client
from sqlalchemy import select

from purse.db.models import Memory
from purse.gateway.mcp import MCPErrorCode
from purse.memory.service import MAX_CONTENT_BYTES
from tests.gateway.conftest import MCP_GOOD_TOKEN, MCPDbVault

pytestmark = pytest.mark.db


def _data(result: mcp_types.CallToolResult) -> dict[str, Any]:
    """The structured payload of a successful tool call."""
    assert not result.is_error, _text(result)
    assert isinstance(result.content[0], mcp_types.TextContent)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def _text(result: mcp_types.CallToolResult) -> str:
    if result.content and isinstance(result.content[0], mcp_types.TextContent):
        return result.content[0].text
    return "<no content>"


def _error(result: mcp_types.CallToolResult) -> dict[str, Any]:
    assert result.is_error
    assert isinstance(result.content[0], mcp_types.TextContent)
    return cast(dict[str, Any], json.loads(result.content[0].text)["error"])


def test_add_search_list_update_delete_round_trip(mcp_db: MCPDbVault) -> None:
    server = mcp_db.build()

    async def body() -> None:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            added = _data(
                await client.call_tool_mcp(
                    "add_memory",
                    {
                        "content": "I prefer TypeScript",
                        "kind": "preference",
                        "initiated_by": "user",
                    },
                )
            )
            memory_id = added["id"]
            assert added["created_at"]

            # search finds it (ILIKE fallback, NullEngine)
            results = _data(await client.call_tool_mcp("search_memory", {"query": "TypeScript"}))[
                "results"
            ]
            assert [hit["id"] for hit in results] == [memory_id]
            assert results[0]["content"] == "I prefer TypeScript"
            assert "provenance" in results[0]

            # list shows it in the current view
            listed = _data(await client.call_tool_mcp("list_memories", {}))["items"]
            assert memory_id in {item["id"] for item in listed}

            # update supersedes → a NEW id
            updated = _data(
                await client.call_tool_mcp(
                    "update_memory", {"id": memory_id, "content": "I prefer Rust"}
                )
            )
            new_id = updated["id"]
            assert new_id != memory_id

            # the old id is gone from the current view; the new one is present
            listed_after = {
                item["id"]
                for item in _data(await client.call_tool_mcp("list_memories", {}))["items"]
            }
            assert memory_id not in listed_after
            assert new_id in listed_after

            # delete tombstones the new head (idempotent)
            deleted = _data(await client.call_tool_mcp("delete_memory", {"id": new_id}))
            assert deleted == {"id": new_id, "deleted": True}
            again = _data(await client.call_tool_mcp("delete_memory", {"id": new_id}))
            assert again["deleted"] is True

            final = {
                item["id"]
                for item in _data(await client.call_tool_mcp("list_memories", {}))["items"]
            }
            assert new_id not in final

    anyio.run(body)


def test_provenance_is_the_token_connection_not_a_client_claim(mcp_db: MCPDbVault) -> None:
    """C4.7: the trusted provenance is the verified connection; agent_id is absent on MCP."""
    server = mcp_db.build()

    async def body() -> str:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            added = _data(
                await client.call_tool_mcp(
                    "add_memory",
                    {"content": "provenance check", "kind": "fact", "initiated_by": "user"},
                )
            )
            return cast(str, added["id"])

    memory_id = uuid.UUID(anyio.run(body))

    with mcp_db.session_factory() as session:
        row = session.scalars(select(Memory).where(Memory.id == memory_id)).one()
        # The connection is the one the token verified as — not anything the client sent.
        assert row.connection_id == mcp_db.connection_id
        assert row.workspace_id == mcp_db.workspace_id
        # initiated_by is the self-reported claim, recorded faithfully.
        assert row.initiated_by.value == "user"
        # MCP has no per-call agent header; agent_id is absent (PRD §10 allows this).
        assert row.agent_id is None


def test_writes_disabled_blocks_add_via_mcp_and_writes_no_row(mcp_db: MCPDbVault) -> None:
    server = mcp_db.build(writes_enabled=False)

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            return _error(
                await client.call_tool_mcp(
                    "add_memory",
                    {"content": "should not persist", "kind": "fact", "initiated_by": "agent"},
                )
            )

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.UNAUTHORIZED_SCOPE

    with mcp_db.session_factory() as session:
        rows = session.scalars(
            select(Memory).where(Memory.workspace_id == mcp_db.workspace_id)
        ).all()
        assert rows == []


def test_oversized_content_maps_to_payload_too_large(mcp_db: MCPDbVault) -> None:
    server = mcp_db.build()
    oversized = "a" * (MAX_CONTENT_BYTES + 1)

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            return _error(
                await client.call_tool_mcp(
                    "add_memory",
                    {"content": oversized, "kind": "fact", "initiated_by": "agent"},
                )
            )

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.PAYLOAD_TOO_LARGE


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("update_memory", {"id": str(uuid.uuid4()), "content": "x"}),
        ("delete_memory", {"id": str(uuid.uuid4())}),
    ],
)
def test_unknown_id_maps_to_not_found(
    mcp_db: MCPDbVault, tool: str, arguments: dict[str, Any]
) -> None:
    server = mcp_db.build()

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            return _error(await client.call_tool_mcp(tool, arguments))

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.NOT_FOUND
