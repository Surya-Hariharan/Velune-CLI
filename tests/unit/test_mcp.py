"""Unit tests for Velune MCP support (server, client, and config)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from velune.kernel.config import VeluneConfig, MCPConfig
from velune.mcp.config import load_mcp_servers
from velune.mcp.server import VeluneMCPServer
from velune.mcp.client import VeluneMCPClient, MCPToolWrapper
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool


class DummyTool(BaseTool):
    def get_name(self) -> str:
        return "dummy_tool"

    def get_description(self) -> str:
        return "A dummy tool for testing"

    async def execute(self, param: str) -> str:
        return f"Executed with {param}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"param": {"type": "string"}},
            "required": ["param"]
        }


def test_load_mcp_servers(tmp_path):
    """Verify that load_mcp_servers parses velune.toml servers block correctly."""
    toml_content = """
[project]
name = "velune"
version = "0.1.0"

[mcp.servers]
github = "https://mcp.github.com/sse"
notion = "https://mcp.notion.com/sse"
"""
    config_file = tmp_path / "velune.toml"
    with open(config_file, "w") as f:
        f.write(toml_content)

    servers = load_mcp_servers(config_file)
    assert servers == {
        "github": "https://mcp.github.com/sse",
        "notion": "https://mcp.notion.com/sse",
    }


def test_load_mcp_servers_missing():
    """Verify load_mcp_servers handles missing configuration gracefully by returning empty dict."""
    servers = load_mcp_servers(Path("/nonexistent/velune.toml"))
    assert servers == {}


@pytest.mark.asyncio
async def test_mcp_server():
    """Verify that VeluneMCPServer correctly lists and executes tools from registry."""
    registry = ToolRegistry()
    registry.register(DummyTool())

    server = VeluneMCPServer(registry)
    
    from mcp.types import ListToolsRequest, CallToolRequest

    # Simulate listing tools
    list_handler = server.server.request_handlers[ListToolsRequest]
    list_result = await list_handler(ListToolsRequest())
    tools = list_result.root.tools
    assert len(tools) == 1
    assert tools[0].name == "dummy_tool"
    assert tools[0].description == "A dummy tool for testing"
    assert tools[0].inputSchema == {
        "type": "object",
        "properties": {"param": {"type": "string"}},
        "required": ["param"]
    }

    # Simulate calling tools
    call_handler = server.server.request_handlers[CallToolRequest]
    call_result = await call_handler(CallToolRequest(params={"name": "dummy_tool", "arguments": {"param": "hello"}}))
    result = call_result.root.content
    assert len(result) == 1
    assert result[0].type == "text"
    assert result[0].text == "Executed with hello"


@pytest.mark.asyncio
async def test_mcp_client_connection():
    """Verify VeluneMCPClient connects to server, fetches tools, and wraps them."""
    mock_tool = MagicMock()
    mock_tool.name = "external_tool"
    mock_tool.description = "An external tool"
    mock_tool.inputSchema = {"type": "object"}

    mock_tools_result = MagicMock()
    mock_tools_result.tools = [mock_tool]

    mock_session = AsyncMock()
    mock_session.list_tools.return_value = mock_tools_result
    mock_session.call_tool.return_value = MagicMock(content=[MagicMock(text="Tool output text")])

    with patch("mcp.client.sse.sse_client", return_value=AsyncMock()) as mock_sse_client:
        mock_sse_client.return_value.__aenter__.return_value = (MagicMock(), MagicMock())
        
        with patch("mcp.ClientSession", return_value=AsyncMock()) as mock_client_session:
            mock_client_session.return_value.__aenter__.return_value = mock_session

            client = VeluneMCPClient("http://fake-server/sse", "test_server")
            
            # Connect
            tools = await client.connect()
            assert len(tools) == 1
            assert tools[0]["name"] == "external_tool"
            assert tools[0]["description"] == "An external tool"

            # Convert to Velune tools
            velune_tools = client.to_velune_tools()
            assert len(velune_tools) == 1
            wrapper_tool = velune_tools[0]
            
            assert wrapper_tool.get_name() == "test_server_external_tool"
            assert wrapper_tool.get_description() == "An external tool"
            assert wrapper_tool.get_schema() == {"type": "object"}

            # Execute wrapper tool
            exec_result = await wrapper_tool.execute(arg="val")
            assert exec_result == "Tool output text"
            mock_session.call_tool.assert_called_once_with("external_tool", arguments={"arg": "val"})

            # Disconnect
            await client.disconnect()
            assert client.session is None
