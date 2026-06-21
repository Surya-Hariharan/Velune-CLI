"""Phase 2 MCP tests — transport layer, registry, and config loading."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.mcp.transports.base import (
    MCPTransportError,
    ServerConfig,
    ToolInfo,
    TransportType,
)
from velune.mcp.transports.factory import make_connection


# ---------------------------------------------------------------------------
# ServerConfig.from_dict
# ---------------------------------------------------------------------------


class TestServerConfig:
    def test_stdio_detected_from_command(self):
        cfg = ServerConfig.from_dict("fs", {"command": "npx", "args": ["-y", "@mcp/fs"]})
        assert cfg.transport == TransportType.STDIO
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "@mcp/fs"]

    def test_sse_detected_from_type(self):
        cfg = ServerConfig.from_dict("github", {"type": "sse", "url": "https://mcp.github.com/sse"})
        assert cfg.transport == TransportType.SSE
        assert cfg.url == "https://mcp.github.com/sse"

    def test_http_type(self):
        cfg = ServerConfig.from_dict("api", {"type": "http", "url": "https://api.example.com/mcp"})
        assert cfg.transport == TransportType.HTTP

    def test_headers_preserved(self):
        cfg = ServerConfig.from_dict(
            "api",
            {"type": "http", "url": "https://x.com/mcp", "headers": {"Authorization": "Bearer tok"}},
        )
        assert cfg.headers["Authorization"] == "Bearer tok"

    def test_env_preserved(self):
        cfg = ServerConfig.from_dict(
            "fs",
            {"command": "python", "args": ["-m", "srv"], "env": {"FOO": "bar"}},
        )
        assert cfg.env["FOO"] == "bar"

    def test_round_trip_stdio(self):
        cfg = ServerConfig.from_dict("fs", {"command": "npx", "args": ["-y", "srv"]})
        d = cfg.to_dict()
        assert d["command"] == "npx"
        assert d["args"] == ["-y", "srv"]

    def test_round_trip_sse(self):
        cfg = ServerConfig.from_dict("gh", {"type": "sse", "url": "https://mcp.github.com/sse"})
        d = cfg.to_dict()
        assert d["type"] == "sse"
        assert d["url"] == "https://mcp.github.com/sse"


# ---------------------------------------------------------------------------
# make_connection factory
# ---------------------------------------------------------------------------


class TestMakeConnection:
    def test_returns_stdio_connection(self):
        from velune.mcp.transports.stdio import StdioConnection

        cfg = ServerConfig(name="x", transport=TransportType.STDIO, command="echo")
        conn = make_connection(cfg)
        assert isinstance(conn, StdioConnection)

    def test_returns_sse_connection(self):
        from velune.mcp.transports.sse import SSEConnection

        cfg = ServerConfig(name="x", transport=TransportType.SSE, url="http://localhost/sse")
        conn = make_connection(cfg)
        assert isinstance(conn, SSEConnection)

    def test_returns_http_connection(self):
        from velune.mcp.transports.http import HTTPConnection

        cfg = ServerConfig(name="x", transport=TransportType.HTTP, url="http://localhost/mcp")
        conn = make_connection(cfg)
        assert isinstance(conn, HTTPConnection)

    def test_returns_websocket_connection(self):
        from velune.mcp.transports.websocket import WebSocketConnection

        cfg = ServerConfig(name="x", transport=TransportType.WEBSOCKET, url="ws://localhost/mcp")
        conn = make_connection(cfg)
        assert isinstance(conn, WebSocketConnection)


# ---------------------------------------------------------------------------
# MCPServerRegistry — config loading
# ---------------------------------------------------------------------------


class TestRegistryConfigLoading:
    def test_load_mcp_json(self, tmp_path: Path):
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(
            json.dumps({
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", str(tmp_path)],
                },
                "github": {
                    "type": "sse",
                    "url": "https://mcp.github.com/sse",
                },
            }),
            encoding="utf-8",
        )

        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        registry.load_config()

        assert "filesystem" in registry._entries
        assert "github" in registry._entries
        assert registry._entries["filesystem"].config.transport == TransportType.STDIO
        assert registry._entries["github"].config.transport == TransportType.SSE

    def test_load_ignores_invalid_entries(self, tmp_path: Path):
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(
            json.dumps({
                "good": {"command": "echo"},
                "bad": "not-a-dict",
            }),
            encoding="utf-8",
        )

        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        registry.load_config()
        assert "good" in registry._entries
        assert "bad" not in registry._entries

    def test_load_missing_file_is_noop(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        registry.load_config()
        assert len(registry._entries) == 0

    def test_register_manual(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        cfg = ServerConfig(name="manual", transport=TransportType.STDIO, command="echo")
        registry.register(cfg)
        assert "manual" in registry._entries

    def test_velune_toml_mcp_servers(self, tmp_path: Path):
        toml = tmp_path / "velune.toml"
        toml.write_text(
            '[mcp.servers]\nfoo = {command = "python", args = ["-m", "foo_srv"]}\n',
            encoding="utf-8",
        )

        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        registry.load_config()
        assert "foo" in registry._entries
        assert registry._entries["foo"].config.command == "python"


# ---------------------------------------------------------------------------
# MCPServerRegistry — connect / disconnect with mocked transport
# ---------------------------------------------------------------------------


def _make_mock_conn(server_name: str, tools: list[str] | None = None) -> MagicMock:
    """Build a mock MCPConnection with list_tools returning given tool names."""
    conn = MagicMock()
    conn.connect = AsyncMock()
    conn.disconnect = AsyncMock()
    tool_list = [
        ToolInfo(name=n, description=f"Tool {n}", input_schema={}, server_name=server_name)
        for n in (tools or ["tool_a", "tool_b"])
    ]
    conn.list_tools = AsyncMock(return_value=tool_list)
    conn.list_resources = AsyncMock(return_value=[])
    conn.call_tool = AsyncMock(return_value="ok")
    return conn


@pytest.mark.asyncio
class TestRegistryConnect:
    async def test_connect_success(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry, ServerState

        registry = MCPServerRegistry(workspace=tmp_path)
        cfg = ServerConfig(name="mock", transport=TransportType.STDIO, command="echo")
        registry.register(cfg)

        mock_conn = _make_mock_conn("mock")
        with patch("velune.mcp.registry.make_connection", return_value=mock_conn):
            ok = await registry.connect("mock")

        assert ok is True
        assert registry._entries["mock"].state == ServerState.CONNECTED
        assert len(registry._entries["mock"].tools) == 2
        mock_conn.connect.assert_called_once()

    async def test_connect_failure_marks_error(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry, ServerState

        registry = MCPServerRegistry(workspace=tmp_path)
        cfg = ServerConfig(name="bad", transport=TransportType.STDIO, command="nonexistent")
        registry.register(cfg)

        failing_conn = MagicMock()
        failing_conn.connect = AsyncMock(side_effect=MCPTransportError("not found"))

        with patch("velune.mcp.registry.make_connection", return_value=failing_conn):
            ok = await registry.connect("bad")

        assert ok is False
        assert registry._entries["bad"].state == ServerState.ERROR
        assert "not found" in registry._entries["bad"].error

    async def test_connect_unknown_server(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        ok = await registry.connect("does-not-exist")
        assert ok is False

    async def test_disconnect(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry, ServerState

        registry = MCPServerRegistry(workspace=tmp_path)
        cfg = ServerConfig(name="srv", transport=TransportType.STDIO, command="echo")
        registry.register(cfg)

        mock_conn = _make_mock_conn("srv")
        with patch("velune.mcp.registry.make_connection", return_value=mock_conn):
            await registry.connect("srv")

        await registry.disconnect("srv")
        assert registry._entries["srv"].state == ServerState.DISCONNECTED
        mock_conn.disconnect.assert_called_once()

    async def test_all_tools_aggregates(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        for name, tool_names in [("a", ["tool1"]), ("b", ["tool2", "tool3"])]:
            cfg = ServerConfig(name=name, transport=TransportType.STDIO, command="echo")
            registry.register(cfg)
            mock_conn = _make_mock_conn(name, tool_names)
            with patch("velune.mcp.registry.make_connection", return_value=mock_conn):
                await registry.connect(name)

        all_tools = registry.all_tools()
        assert len(all_tools) == 3
        names = {t.name for t in all_tools}
        assert {"tool1", "tool2", "tool3"} == names

    async def test_call_tool_routes_correctly(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        cfg = ServerConfig(name="fs", transport=TransportType.STDIO, command="echo")
        registry.register(cfg)

        mock_conn = _make_mock_conn("fs", ["read_file"])
        mock_conn.call_tool = AsyncMock(return_value="file contents")
        with patch("velune.mcp.registry.make_connection", return_value=mock_conn):
            await registry.connect("fs")

        result = await registry.call_tool("fs_read_file", {"path": "/tmp/x"})
        assert result == "file contents"
        mock_conn.call_tool.assert_called_once_with("read_file", {"path": "/tmp/x"})

    async def test_connect_all(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        for name in ("a", "b", "c"):
            registry.register(ServerConfig(name=name, transport=TransportType.STDIO, command="echo"))

        mock_conn = _make_mock_conn("any")
        with patch("velune.mcp.registry.make_connection", return_value=mock_conn):
            results = await registry.connect_all()

        assert all(results.values()), f"Some servers failed: {results}"

    async def test_status_reflects_state(self, tmp_path: Path):
        from velune.mcp.registry import MCPServerRegistry

        registry = MCPServerRegistry(workspace=tmp_path)
        registry.register(ServerConfig(name="srv", transport=TransportType.STDIO, command="echo"))

        statuses = registry.status()
        assert len(statuses) == 1
        assert statuses[0]["state"] == "disconnected"
        assert statuses[0]["name"] == "srv"


# ---------------------------------------------------------------------------
# VeluneMCPClient backward compat
# ---------------------------------------------------------------------------


class TestVeluneMCPClientBackwardCompat:
    def test_from_url_creates_sse_config(self):
        from velune.mcp.client import VeluneMCPClient

        client = VeluneMCPClient("http://localhost:7788/sse", "test")
        assert client._config.transport == TransportType.SSE
        assert client._config.url == "http://localhost:7788/sse"
        assert client.server_name == "test"

    def test_from_config_stdio(self):
        from velune.mcp.client import VeluneMCPClient

        client = VeluneMCPClient.from_config(
            {"command": "npx", "args": ["-y", "@mcp/fs"]},
            name="fs",
        )
        assert client._config.transport == TransportType.STDIO
        assert client._config.command == "npx"

    @pytest.mark.asyncio
    async def test_connect_returns_tool_dicts(self):
        from velune.mcp.client import VeluneMCPClient

        client = VeluneMCPClient("http://localhost:7788/sse", "test")
        mock_conn = _make_mock_conn("test", ["do_thing"])
        with patch("velune.mcp.client.make_connection", return_value=mock_conn):
            tools = await client.connect()

        assert len(tools) == 1
        assert tools[0]["name"] == "do_thing"
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_to_velune_tools(self):
        from velune.mcp.client import VeluneMCPClient

        client = VeluneMCPClient("http://localhost:7788/sse", "test")
        mock_conn = _make_mock_conn("test", ["do_thing"])
        with patch("velune.mcp.client.make_connection", return_value=mock_conn):
            await client.connect()

        wrapped = client.to_velune_tools()
        assert len(wrapped) == 1
        # Verify duck-type BaseTool interface
        assert wrapped[0].get_name() == "test_do_thing"
        assert wrapped[0].get_description() == "Tool do_thing"
        assert hasattr(wrapped[0], "execute")
        await client.disconnect()
