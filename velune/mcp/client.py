"""MCP client — connects to external MCP servers and exposes them as Velune tools.

``VeluneMCPClient`` is the original single-server API (backward compatible).
New code should prefer :class:`velune.mcp.registry.MCPServerRegistry` for
multi-server setups.

Transport is now selected automatically based on server config:
- Provide ``command`` (+ optional ``args``) → stdio subprocess transport
- Provide ``url`` with ``type: "sse"`` (default for URLs) → SSE transport
- Provide ``url`` with ``type: "http"`` → HTTP streamable transport
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from velune.mcp.transports.base import ServerConfig, ToolInfo, TransportType
from velune.mcp.transports.factory import make_connection

if TYPE_CHECKING:
    from velune.tools.base.tool import BaseTool

logger = logging.getLogger("velune.mcp.client")


def _base_tool_cls() -> type:
    """Lazy import of BaseTool to avoid CLI-layer circular imports."""
    from velune.tools.base.tool import BaseTool  # noqa: PLC0415

    return BaseTool


class _MCPToolWrapperBase:
    """Mixin holding the implementation; actual base class resolved lazily."""

    def __init__(
        self,
        client: VeluneMCPClient,
        name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> None:
        self._client = client
        self._name = name
        self._description = description
        self._input_schema = input_schema

    def get_name(self) -> str:
        return f"{self._client.server_name}_{self._name}"

    def get_description(self) -> str:
        return self._description

    def get_schema(self) -> dict[str, Any]:
        return self._input_schema

    async def execute(self, **kwargs: Any) -> Any:
        if self._client._connection is None:
            raise RuntimeError(f"MCP client '{self._client.server_name}' is not connected.")
        return await self._client._connection.call_tool(self._name, kwargs)


def _make_wrapper_class() -> type:
    """Build MCPToolWrapper inheriting from BaseTool (deferred until first use)."""
    base = _base_tool_cls()

    class MCPToolWrapper(_MCPToolWrapperBase, base):  # type: ignore[misc]
        """Wraps an external MCP tool as a Velune BaseTool."""

    return MCPToolWrapper


_MCPToolWrapper: type | None = None


def MCPToolWrapper(  # noqa: N802
    client: VeluneMCPClient,
    name: str,
    description: str,
    input_schema: dict[str, Any],
) -> Any:
    """Factory that creates an MCPToolWrapper (BaseTool subclass) on first call."""
    global _MCPToolWrapper
    if _MCPToolWrapper is None:
        _MCPToolWrapper = _make_wrapper_class()
    return _MCPToolWrapper(client, name, description, input_schema)


class VeluneMCPClient:
    """Single-server MCP client with automatic transport selection.

    Supports the original SSE-URL usage::

        client = VeluneMCPClient("http://localhost:7788/sse", "myserver")
        tools = await client.connect()
        result = await client.call_tool("my_tool", {"key": "value"})
        await client.disconnect()

    And the new config-dict usage for stdio servers::

        client = VeluneMCPClient.from_config({
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        }, name="filesystem")
    """

    def __init__(
        self,
        server_url: str,
        server_name: str,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.server_url = server_url
        self.server_name = server_name
        self._allowed_hosts = allowed_hosts
        self._connection = None
        self._raw_tools: list[ToolInfo] = []
        # Build config from the legacy URL-only signature
        self._config = ServerConfig(
            name=server_name,
            transport=TransportType.SSE,
            url=server_url,
        )

    @classmethod
    def from_config(
        cls,
        config_dict: dict[str, Any],
        name: str,
        allowed_hosts: list[str] | None = None,
    ) -> VeluneMCPClient:
        """Build a client from a raw ``.mcp.json`` entry dict."""
        client = cls.__new__(cls)
        client.server_name = name
        client._allowed_hosts = allowed_hosts
        client._connection = None
        client._raw_tools = []
        client._config = ServerConfig.from_dict(name, config_dict)
        client.server_url = client._config.url
        return client

    async def connect(self) -> list[dict[str, Any]]:
        """Connect and return list of available tool dicts."""
        conn = make_connection(self._config, allowed_hosts=self._allowed_hosts)
        await conn.connect()
        self._connection = conn
        self._raw_tools = await conn.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self._raw_tools
        ]

    def to_velune_tools(self) -> list[BaseTool]:
        """Convert MCP tools to Velune BaseTool wrappers."""
        return [
            MCPToolWrapper(
                client=self,
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
            )
            for t in self._raw_tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Directly call a tool by its raw (un-prefixed) name."""
        if self._connection is None:
            raise RuntimeError(f"MCP client '{self.server_name}' is not connected.")
        return await self._connection.call_tool(tool_name, arguments)

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._connection is not None:
            try:
                await self._connection.disconnect()
            except Exception as exc:
                logger.debug("Disconnect error for '%s': %s", self.server_name, exc)
            self._connection = None
            self._raw_tools = []
