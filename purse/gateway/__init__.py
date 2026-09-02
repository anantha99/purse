"""Gateway (C4): the MCP server, transport, and tool contracts — Purse's only public surface.

:mod:`purse.gateway.rest` lands first (C3.8): a small FastAPI app over the memory
service, so M1 can prove the write→read spine with curl before MCP exists. The
FastMCP server (C4.1) joins it and shares the same service functions and the same
:mod:`purse.gateway.errors` codes.

Nothing here is imported eagerly: ``purse.gateway.rest`` pulls in FastAPI, and
``purse.db`` tooling (migrations, export) has no business paying for that import.
"""
