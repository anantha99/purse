"""MCP tool surface: registration, schemas, and authorization — no database (C4.6).

Everything here is provable without a row existing: the five tools are present
with the right shapes, no tool accepts ``workspace_id``/``connection_id`` (PRD §10),
scope denial and writes-off produce the structured ``UNAUTHORIZED_SCOPE`` envelope,
and an unauthenticated call is refused. The DB round trip lives in
``test_mcp_db.py``.

Two transports are used deliberately:

* the **in-memory** transport (``Client(server)``) for schema/registration and the
  unauthenticated path — it does not run the HTTP auth stack, so it is exactly the
  right tool for "what does this server expose" and "what happens with no token";
* the **in-process HTTP** transport (``asgi_client``) for anything that must go
  through token verification — scope checks read the *verified* token, which only
  exists once the bearer auth middleware has run.

The scope-denial tests wire a ``session_factory`` that raises: reaching it would
mean authorization ran *after* a database session was opened, which would be the
wrong order (PRD §12: auth → scope → insert).
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

TOOL_NAMES = {"search_memory", "add_memory", "list_memories", "update_memory", "delete_memory"}


def _exploding_factory() -> Any:
    """A session factory that must never be called (authorization should gate first)."""

    def factory() -> Any:
        raise AssertionError("the database was reached before authorization")

    return factory


def _envelope(result: mcp_types.CallToolResult) -> dict[str, Any]:
    """The parsed PRD §10 error envelope from an ``isError`` tool result."""
    assert result.is_error
    assert isinstance(result.content[0], mcp_types.TextContent)
    payload = cast(dict[str, Any], json.loads(result.content[0].text))
    assert set(payload["error"]) == {"code", "message"}
    return cast(dict[str, Any], payload["error"])


# ---------------------------------------------------------------------------
# Registration & schemas (in-memory transport)
# ---------------------------------------------------------------------------


def test_all_five_memory_tools_registered() -> None:
    server = create_mcp_server(session_factory=_exploding_factory(), engine=NullEngine(), auth=None)

    async def body() -> set[str]:
        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}

    assert anyio.run(body) == TOOL_NAMES


@pytest.mark.parametrize("tool_name", sorted(TOOL_NAMES))
def test_no_tool_accepts_workspace_or_connection_argument(tool_name: str) -> None:
    """PRD §10: workspace is resolved from the token, never a tool parameter."""
    server = create_mcp_server(session_factory=_exploding_factory(), engine=NullEngine(), auth=None)

    async def body() -> dict[str, Any]:
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            return cast(dict[str, Any], tools[tool_name].input_schema)

    schema = anyio.run(body)
    properties = set(schema.get("properties", {}))
    assert "workspace_id" not in properties
    assert "connection_id" not in properties
    # No forgotten identity fields sneak in under another spelling either.
    assert not {p for p in properties if "workspace" in p or "connection" in p}


def test_tool_schemas_match_prd_contracts() -> None:
    server = create_mcp_server(session_factory=_exploding_factory(), engine=NullEngine(), auth=None)

    async def body() -> dict[str, Any]:
        async with Client(server) as client:
            return {tool.name: tool.input_schema for tool in await client.list_tools()}

    schemas = anyio.run(body)

    search = schemas["search_memory"]
    assert search["properties"]["limit"]["default"] == 8
    assert search["required"] == ["query"]

    add = schemas["add_memory"]
    assert add["properties"]["kind"]["enum"] == ["fact", "preference", "decision"]
    assert add["properties"]["initiated_by"]["enum"] == ["user", "agent"]
    assert set(add["required"]) == {"content", "kind", "initiated_by"}

    assert set(schemas["update_memory"]["required"]) == {"id", "content"}
    assert schemas["delete_memory"]["required"] == ["id"]
    # list_memories paginates with an optional cursor.
    assert "cursor" in schemas["list_memories"]["properties"]
    assert "cursor" not in schemas["list_memories"].get("required", [])


# ---------------------------------------------------------------------------
# Authorization (in-process HTTP transport, through token verification)
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("add_memory", {"content": "x", "kind": "fact", "initiated_by": "agent"}),
        ("update_memory", {"id": str(uuid.uuid4()), "content": "x"}),
        ("delete_memory", {"id": str(uuid.uuid4())}),
    ],
)
def test_write_tools_denied_without_write_scope(tool: str, arguments: dict[str, Any]) -> None:
    """A read-only connection is refused every write, before any DB work."""
    server = _server_with(scopes=("memory:read",))

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            result = await client.call_tool_mcp(tool, arguments)
            return _envelope(result)

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.UNAUTHORIZED_SCOPE


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_memory", {"query": "anything"}),
        ("list_memories", {}),
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


def test_writes_disabled_blocks_write_even_when_scope_granted() -> None:
    """The "writes on" badge (PRD §7.1): writes_enabled=False cuts a granted write."""
    server = _server_with(scopes=("memory:read", "memory:write"), writes_enabled=False)

    async def body() -> dict[str, Any]:
        async with asgi_client(server, auth=MCP_GOOD_TOKEN) as client:
            result = await client.call_tool_mcp(
                "add_memory", {"content": "x", "kind": "fact", "initiated_by": "agent"}
            )
            return _envelope(result)

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.UNAUTHORIZED_SCOPE
    assert "writes are disabled" in error["message"]


def test_bad_bearer_token_is_refused_at_the_transport() -> None:
    """An unverifiable token never reaches a tool — the HTTP layer rejects it (401)."""
    server = _server_with(scopes=("memory:read",))

    async def body() -> None:
        with pytest.raises(Exception):  # noqa: B017 - any transport-level rejection is acceptable
            async with asgi_client(server, auth="not-the-token") as client:
                await client.list_tools()

    anyio.run(body)


def test_unauthenticated_call_yields_structured_error() -> None:
    """With no auth provider, get_access_token() is empty and every tool refuses.

    Uses the in-memory transport, which does not run the auth stack — so this is
    the defensive ``UNAUTHENTICATED`` branch, not something a real deployment hits.
    """
    server = create_mcp_server(session_factory=_exploding_factory(), engine=NullEngine(), auth=None)

    async def body() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool_mcp("search_memory", {"query": "x"})
            return _envelope(result)

    error = anyio.run(body)
    assert error["code"] == MCPErrorCode.UNAUTHENTICATED
