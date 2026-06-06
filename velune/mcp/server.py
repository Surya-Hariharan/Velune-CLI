"""MCP server exposing Velune tools to external clients."""

from __future__ import annotations

import time

import mcp.server.stdio
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import TextContent, Tool

from velune.tools.base.registry import ToolRegistry

DEFAULT_HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 1 * 1024 * 1024  # 1 MB per request


class RateLimiter:
    """Token-bucket rate limiter keyed by client ID.

    Each client starts with a full bucket.  Tokens refill at *calls_per_minute*
    tokens per minute.  Once the bucket empties, calls are rejected until
    enough time passes to accumulate another token.
    """

    def __init__(self, calls_per_minute: int = 60) -> None:
        self._limit = calls_per_minute
        self._tokens: dict[str, float] = {}
        self._last_check: dict[str, float] = {}

    def is_allowed(self, client_id: str = "default") -> bool:
        now = time.monotonic()
        if client_id not in self._tokens:
            # First call — bucket starts full so the client isn't immediately blocked.
            self._tokens[client_id] = float(self._limit)
            self._last_check[client_id] = now
        elapsed = now - self._last_check[client_id]
        self._last_check[client_id] = now
        self._tokens[client_id] = min(
            float(self._limit),
            self._tokens[client_id] + elapsed * (self._limit / 60.0),
        )
        if self._tokens[client_id] >= 1.0:
            self._tokens[client_id] -= 1.0
            return True
        return False


class VeluneMCPServer:
    """Exposes Velune's ToolRegistry as an MCP server."""

    def __init__(self, tool_registry: ToolRegistry, calls_per_minute: int = 60):
        self.tool_registry = tool_registry
        self.server = Server("velune")
        self._rate_limiter = RateLimiter(calls_per_minute=calls_per_minute)
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
            if not self._rate_limiter.is_allowed():
                raise ValueError("Rate limit exceeded — too many tool calls per minute.")
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
