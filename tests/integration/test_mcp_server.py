"""Integration tests for MCP stdio transport.

These tests spawn a real stdio MCP server subprocess and verify the full
round-trip: connect → list_tools → call_tool → disconnect.

Run with: pytest tests/integration/ -v -m integration
Exclude with: pytest -m 'not integration'
"""

import sys
from pathlib import Path

import pytest

from velune.mcp.transports.base import ServerConfig, TransportType
from velune.mcp.transports.stdio import StdioConnection

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "simple_mcp_server.py")


def _make_config() -> ServerConfig:
    return ServerConfig(
        name="simple",
        command=sys.executable,
        args=[FIXTURE_SERVER],
        transport=TransportType.STDIO,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stdio_list_tools():
    """Spawn the fixture server, list tools, verify 'add' is present."""
    conn = StdioConnection(_make_config())
    try:
        await conn.connect()
        tools = await conn.list_tools()
    finally:
        await conn.disconnect()

    names = [t.name for t in tools]
    assert "add" in names, f"Expected 'add' in tool list, got: {names}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stdio_call_tool():
    """Spawn the fixture server, call 'add(2, 3)', verify result == '5'."""
    conn = StdioConnection(_make_config())
    try:
        await conn.connect()
        result = await conn.call_tool("add", {"a": 2, "b": 3})
    finally:
        await conn.disconnect()

    assert result.strip() == "5", f"Expected '5', got: {result!r}"
