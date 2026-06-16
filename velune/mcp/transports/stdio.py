"""stdio transport — launch a local MCP server as a subprocess.

This is the most common transport for community MCP servers (e.g.
``@modelcontextprotocol/server-filesystem``, ``mcp-server-sqlite``).
The subprocess receives JSON-RPC messages on its stdin and writes responses
to its stdout; stderr is captured as debug logging.

Example config (in ``.mcp.json``)::

    {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"LOG_LEVEL": "debug"}
      }
    }
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from velune.mcp.transports.base import (
    MCPConnection,
    MCPTransportError,
    ResourceInfo,
    ServerConfig,
    ToolInfo,
)

logger = logging.getLogger("velune.mcp.transports.stdio")


class StdioConnection(MCPConnection):
    """Live connection to a stdio-based MCP server subprocess."""

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(config)
        self._transport_ctx = None
        self._session_ctx = None
        self._session = None

    async def connect(self) -> None:
        """Spawn the subprocess and initialise the MCP session."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        if not self.config.command:
            raise MCPTransportError(
                f"stdio server '{self.config.name}' has no 'command' configured."
            )

        proc_env = {**os.environ}
        proc_env.update(self.config.env)

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=proc_env,
        )

        logger.debug(
            "stdio connect: %s %s",
            self.config.command,
            " ".join(self.config.args),
        )

        try:
            self._transport_ctx = stdio_client(params)
            read, write = await self._transport_ctx.__aenter__()
        except FileNotFoundError:
            raise MCPTransportError(
                f"stdio command not found: '{self.config.command}'. "
                "Is the server installed?"
            )
        except Exception as exc:
            raise MCPTransportError(
                f"stdio server '{self.config.name}' failed to start: {exc}"
            ) from exc

        try:
            self._session_ctx = ClientSession(read, write)
            self._session = await self._session_ctx.__aenter__()
            await self._session.initialize()
        except Exception as exc:
            # Clean up the transport if session init fails
            try:
                await self._transport_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            raise MCPTransportError(
                f"MCP session init failed for '{self.config.name}': {exc}"
            ) from exc

        logger.info("stdio MCP server '%s' connected.", self.config.name)

    async def disconnect(self) -> None:
        """Terminate the subprocess and clean up."""
        for ctx in (self._session_ctx, self._transport_ctx):
            if ctx is not None:
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception as exc:
                    logger.debug("Disconnect error for '%s': %s", self.config.name, exc)
        self._session = None
        self._session_ctx = None
        self._transport_ctx = None
        logger.debug("stdio MCP server '%s' disconnected.", self.config.name)

    async def list_tools(self) -> list[ToolInfo]:
        """List tools offered by the server."""
        if self._session is None:
            raise MCPTransportError(f"Not connected to '{self.config.name}'.")
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            raise MCPTransportError(f"list_tools failed for '{self.config.name}': {exc}") from exc
        return [
            ToolInfo(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema or {},
                server_name=self.config.name,
            )
            for t in result.tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool and return concatenated text content."""
        if self._session is None:
            raise MCPTransportError(f"Not connected to '{self.config.name}'.")
        try:
            result = await self._session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:
            raise MCPTransportError(
                f"call_tool '{tool_name}' failed on '{self.config.name}': {exc}"
            ) from exc
        return _extract_text(result)

    async def list_resources(self) -> list[ResourceInfo]:
        """List resources offered by the server (if supported)."""
        if self._session is None:
            return []
        try:
            result = await self._session.list_resources()
            return [
                ResourceInfo(
                    uri=str(r.uri),
                    name=r.name,
                    description=getattr(r, "description", "") or "",
                    mime_type=getattr(r, "mimeType", "") or "",
                    server_name=self.config.name,
                )
                for r in result.resources
            ]
        except Exception as exc:
            logger.debug("list_resources not supported by '%s': %s", self.config.name, exc)
            return []

    async def read_resource(self, uri: str) -> str:
        """Read a resource by URI."""
        if self._session is None:
            raise MCPTransportError(f"Not connected to '{self.config.name}'.")
        try:
            result = await self._session.read_resource(uri)
            parts = []
            for content in getattr(result, "contents", []):
                if hasattr(content, "text"):
                    parts.append(content.text)
            return "\n".join(parts)
        except Exception as exc:
            raise MCPTransportError(
                f"read_resource '{uri}' failed on '{self.config.name}': {exc}"
            ) from exc


def _extract_text(result: Any) -> str:
    """Pull text from an MCP CallToolResult."""
    parts = []
    for content in getattr(result, "content", []):
        if hasattr(content, "text"):
            parts.append(content.text)
        elif isinstance(content, dict) and "text" in content:
            parts.append(content["text"])
        else:
            parts.append(str(content))
    return "\n".join(parts) if parts else str(result)
