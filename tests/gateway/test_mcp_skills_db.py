"""Skills MCP tools end to end against real Postgres (C4.4, C5.3) — db-marked.

The full round trip through the three tools over the in-process HTTP transport
(so token verification runs), plus the version/idempotency rule and workspace
isolation, which only a real database proves. Skipped locally; run in CI with
``REQUIRE_DB=1``.
"""

from __future__ import annotations

import json
from typing import Any, cast

import anyio
import mcp.types as mcp_types
import pytest
from fastmcp.utilities.tests import asgi_client

from purse.gateway.mcp import MCPErrorCode
from tests.gateway.conftest import MCP_GOOD_TOKEN, MCPDbVault

pytestmark = pytest.mark.db

_SKILL_SCOPES = ("skills:read", "skills:write")


def _doc(name: str, version: str, *, body: str = "body text") -> str:
    return f"---\nname: {name}\ndescription: A {name} skill.\nversion: {version}\n---\n{body}\n"


def _data(result: mcp_types.CallToolResult) -> dict[str, Any]:
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


def test_upsert_get_list_round_trip(mcp_db: MCPDbVault) -> None:
    server = mcp_db.build(scopes=_SKILL_SCOPES)

    async def body() -> None:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            created = _data(
                await client.call_tool_mcp(
                    "upsert_skill", {"name": "deploy", "content": _doc("deploy", "1.0.0")}
                )
            )
            assert created == {"name": "deploy", "version": "1.0.0"}

            fetched = _data(await client.call_tool_mcp("get_skill", {"name": "deploy"}))
            assert fetched["version"] == "1.0.0"
            assert fetched["frontmatter"]["name"] == "deploy"
            assert fetched["body"].strip() == "body text"

            listed = _data(await client.call_tool_mcp("list_skills", {}))["skills"]
            assert {
                "name": "deploy",
                "description": "A deploy skill.",
                "version": "1.0.0",
            } in listed

            # A new version bumps the head; a pinned get still resolves the old one.
            bumped = _data(
                await client.call_tool_mcp(
                    "upsert_skill",
                    {"name": "deploy", "content": _doc("deploy", "2.0.0", body="v2")},
                )
            )
            assert bumped["version"] == "2.0.0"
            assert (
                _data(await client.call_tool_mcp("get_skill", {"name": "deploy"}))["version"]
                == "2.0.0"
            )
            pinned = _data(
                await client.call_tool_mcp("get_skill", {"name": "deploy", "version": "1.0.0"})
            )
            assert pinned["version"] == "1.0.0"

    anyio.run(body)


def test_reupsert_identical_is_idempotent_same_version_conflict_rejected(
    mcp_db: MCPDbVault,
) -> None:
    server = mcp_db.build(scopes=_SKILL_SCOPES)

    async def body() -> None:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            doc = _doc("deploy", "1.0.0", body="one")
            first = _data(
                await client.call_tool_mcp("upsert_skill", {"name": "deploy", "content": doc})
            )
            again = _data(
                await client.call_tool_mcp("upsert_skill", {"name": "deploy", "content": doc})
            )
            assert first == again == {"name": "deploy", "version": "1.0.0"}

            # Same version, different content → VALIDATION.
            conflict = _error(
                await client.call_tool_mcp(
                    "upsert_skill",
                    {"name": "deploy", "content": _doc("deploy", "1.0.0", body="two")},
                )
            )
            assert conflict["code"] == MCPErrorCode.VALIDATION

    anyio.run(body)


def test_get_unknown_skill_is_not_found(mcp_db: MCPDbVault) -> None:
    server = mcp_db.build(scopes=_SKILL_SCOPES)

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            return _error(await client.call_tool_mcp("get_skill", {"name": "missing"}))

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.NOT_FOUND


def test_name_mismatch_is_validation(mcp_db: MCPDbVault) -> None:
    server = mcp_db.build(scopes=_SKILL_SCOPES)

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            return _error(
                await client.call_tool_mcp(
                    "upsert_skill", {"name": "deploy", "content": _doc("other", "1.0.0")}
                )
            )

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.VALIDATION


def test_oversized_skill_is_payload_too_large(mcp_db: MCPDbVault) -> None:
    from purse.skills.parse import MAX_CONTENT_BYTES

    server = mcp_db.build(scopes=_SKILL_SCOPES)
    oversized = _doc("deploy", "1.0.0", body="a" * (MAX_CONTENT_BYTES + 1))

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            return _error(
                await client.call_tool_mcp("upsert_skill", {"name": "deploy", "content": oversized})
            )

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.PAYLOAD_TOO_LARGE
