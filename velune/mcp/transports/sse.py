"""SSE (Server-Sent Events) transport for hosted MCP servers.

This wraps the existing mcp SDK sse_client, adding SSRF protection from
:mod:`velune.mcp.security` and resource support.

Example config (in ``.mcp.json``)::

    {
      "github": {
        "type": "sse",
        "url": "https://mcp.github.com/sse"
      }
    }
"""

from __future__ import annotations

import logging
from typing import Any

from velune.mcp.transports.base import (
    MCPConnection,
    MCPTransportError,
    ResourceInfo,
    ServerConfig,
    ToolInfo,
)

logger = logging.getLogger("velune.mcp.transports.sse")


class SSEConnection(MCPConnection):
    """Live connection to an SSE-based MCP server."""

    def __init__(
        self,
        config: ServerConfig,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(config)
        self._allowed_hosts = allowed_hosts
        self._sse_ctx = None
        self._session_ctx = None
        self._session = None

    async def connect(self) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        from velune.mcp.security import validate_mcp_url

        url = self.config.url
        if not url:
            raise MCPTransportError(f"SSE server '{self.config.name}' has no 'url' configured.")

        validate_mcp_url(url, self._allowed_hosts)

        # Inject custom headers into the SSE connection if needed
        kwargs: dict[str, Any] = {"url": url}
        if self.config.headers:
            kwargs["headers"] = self.config.headers

        logger.debug("SSE connect: %s", url)
        try:
            self._sse_ctx = sse_client(**kwargs)
            read, write = await self._sse_ctx.__aenter__()
        except Exception as exc:
            raise MCPTransportError(
                f"SSE connection failed for '{self.config.name}' ({url}): {exc}"
            ) from exc

        try:
            self._session_ctx = ClientSession(read, write)
            self._session = await self._session_ctx.__aenter__()
            await self._session.initialize()
        except Exception as exc:
            try:
                await self._sse_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            raise MCPTransportError(
                f"MCP session init failed for '{self.config.name}': {exc}"
            ) from exc

        logger.info("SSE MCP server '%s' connected at %s.", self.config.name, url)

    async def disconnect(self) -> None:
        for ctx in (self._session_ctx, self._sse_ctx):
            if ctx is not None:
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception as exc:
                    logger.debug("Disconnect error for '%s': %s", self.config.name, exc)
        self._session = None
        self._session_ctx = None
        self._sse_ctx = None

    async def list_tools(self) -> list[ToolInfo]:
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
        if self._session is None:
            raise MCPTransportError(f"Not connected to '{self.config.name}'.")
        try:
            result = await self._session.read_resource(uri)
            parts = [
                content.text
                for content in getattr(result, "contents", [])
                if hasattr(content, "text")
            ]
            return "\n".join(parts)
        except Exception as exc:
            raise MCPTransportError(
                f"read_resource '{uri}' failed on '{self.config.name}': {exc}"
            ) from exc


def _extract_text(result: Any) -> str:
    parts = []
    for content in getattr(result, "content", []):
        if hasattr(content, "text"):
            parts.append(content.text)
        elif isinstance(content, dict) and "text" in content:
            parts.append(content["text"])
        else:
            parts.append(str(content))
    return "\n".join(parts) if parts else str(result)
