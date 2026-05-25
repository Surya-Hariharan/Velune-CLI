"""MCP server exposing Velune tools to external clients."""

from __future__ import annotations

import mcp.server.stdio
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import TextContent, Tool

from velune.tools.base.registry import ToolRegistry


class VeluneMCPServer:
    """Exposes Velune's ToolRegistry as an MCP server."""

    def __init__(self, tool_registry: ToolRegistry):
        self.tool_registry = tool_registry
        self.server = Server("velune")
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name=schema["name"],
                    description=schema["description"],
                    inputSchema=schema["schema"],
                )
                for schema in self.tool_registry.list_tool_schemas()
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            tool = self.tool_registry.get(name)
            if not tool:
                raise ValueError(f"Tool not found: {name}")
            result = await tool.execute(**arguments)
            return [TextContent(type="text", text=str(result))]

    async def run_stdio(self) -> None:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="velune",
                    server_version="0.1.0",
                    capabilities={"tools": {}}
                )
            )
