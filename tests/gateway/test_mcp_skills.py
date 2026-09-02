"""Skills MCP tools: registration, schemas, and authorization — no database (C4.4).

Provable without a row existing: the three skills tools are present with the right
shapes, no tool accepts ``workspace_id``/``connection_id`` (PRD §10), and scope
denial produces the structured ``UNAUTHORIZED_SCOPE`` envelope before any DB work.
The round trip lives in ``test_mcp_skills_db.py``.

Two transports, exactly as the memory tool tests use them: the in-memory
transport for "what does this server expose", and the in-process HTTP transport
for anything that must pass through token verification.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import anyio
import mcp.types as mcp_types
import pytest
from fastmcp import Client
from fastmcp.utilities.tests import asgi_client

from purse.gateway.mcp import MCPErrorCode, create_mcp_server
from purse.memory.engine import NullEngine
from tests.gateway.conftest import MCP_GOOD_TOKEN, FakeMCPVerifier

SKILL_TOOL_NAMES = {"list_skills", "get_skill", "upsert_skill"}


def _exploding_factory() -> Any:
    def factory() -> Any:
        raise AssertionError("the database was reached before authorization")

    return factory


def _envelope(result: mcp_types.CallToolResult) -> dict[str, Any]:
    assert result.is_error
    assert isinstance(result.content[0], mcp_types.TextContent)
    payload = cast(dict[str, Any], json.loads(result.content[0].text))
    assert set(payload["error"]) == {"code", "message"}
    return cast(dict[str, Any], payload["error"])


def _server_with(*, scopes: tuple[str, ...], writes_enabled: bool = True) -> Any:
    verifier = FakeMCPVerifier(
        connection_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        scopes=scopes,
        writes_enabled=writes_enabled,
    )
    return create_mcp_server(
        session_factory=_exploding_factory(), engine=NullEngine(), auth=verifier
    )


# ---------------------------------------------------------------------------
# Registration & schemas (in-memory transport)
# ---------------------------------------------------------------------------


def test_skills_tools_registered_alongside_memory_tools() -> None:
    server = create_mcp_server(session_factory=_exploding_factory(), engine=NullEngine(), auth=None)

    async def body() -> set[str]:
        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}

    names = anyio.run(body)
    assert names >= SKILL_TOOL_NAMES
    # The memory tools are still there — skills are added, not swapped in.
    assert names >= {"search_memory", "add_memory"}


@pytest.mark.parametrize("tool_name", sorted(SKILL_TOOL_NAMES))
def test_no_skills_tool_accepts_workspace_or_connection_argument(tool_name: str) -> None:
    server = create_mcp_server(session_factory=_exploding_factory(), engine=NullEngine(), auth=None)

    async def body() -> dict[str, Any]:
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            return cast(dict[str, Any], tools[tool_name].input_schema)

    schema = anyio.run(body)
    properties = set(schema.get("properties", {}))
    assert not {p for p in properties if "workspace" in p or "connection" in p}


def test_skills_tool_schemas_match_prd_contracts() -> None:
    server = create_mcp_server(session_factory=_exploding_factory(), engine=NullEngine(), auth=None)

    async def body() -> dict[str, Any]:
        async with Client(server) as client:
            return {tool.name: tool.input_schema for tool in await client.list_tools()}

    schemas = anyio.run(body)

    # list_skills: no parameters.
    assert schemas["list_skills"].get("properties", {}) == {}

    # get_skill: name required, version optional.
    get_skill = schemas["get_skill"]
    assert get_skill["required"] == ["name"]
    assert "version" in get_skill["properties"]
    assert "version" not in get_skill.get("required", [])

    # upsert_skill: name and content, both required.
    assert set(schemas["upsert_skill"]["required"]) == {"name", "content"}


# ---------------------------------------------------------------------------
# Authorization (in-process HTTP transport, through token verification)
# ---------------------------------------------------------------------------


def test_upsert_denied_without_write_scope() -> None:
    """A read-only connection is refused the write, before any DB work."""
    server = _server_with(scopes=("skills:read",))

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            result = await client.call_tool_mcp(
                "upsert_skill", {"name": "x", "content": "irrelevant"}
            )
            return _envelope(result)

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.UNAUTHORIZED_SCOPE


def test_upsert_denied_when_writes_disabled_even_with_scope() -> None:
    """The "writes on" badge (PRD §7.1) cuts a granted skills write."""
    server = _server_with(scopes=("skills:read", "skills:write"), writes_enabled=False)

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            result = await client.call_tool_mcp(
                "upsert_skill", {"name": "x", "content": "irrelevant"}
            )
            return _envelope(result)

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.UNAUTHORIZED_SCOPE
    assert "writes are disabled" in error["message"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_skills", {}),
        ("get_skill", {"name": "anything"}),
    ],
)
def test_read_tools_denied_without_read_scope(tool: str, arguments: dict[str, Any]) -> None:
    server = _server_with(scopes=())

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            result = await client.call_tool_mcp(tool, arguments)
            return _envelope(result)

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.UNAUTHORIZED_SCOPE
