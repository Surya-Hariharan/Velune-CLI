"""HTTP (streamable HTTP) transport for REST-based MCP servers.

Uses the MCP SDK's streamable HTTP client when available, otherwise falls back
to a direct JSON-RPC-over-HTTP implementation.

Example config (in ``.mcp.json``)::

    {
      "my-api": {
        "type": "http",
        "url": "https://api.example.com/mcp",
        "headers": {
          "Authorization": "Bearer ${API_TOKEN}"
        }
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

logger = logging.getLogger("velune.mcp.transports.http")


class HTTPConnection(MCPConnection):
    """Live connection to an HTTP-based MCP server."""

    def __init__(
        self,
        config: ServerConfig,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(config)
        self._allowed_hosts = allowed_hosts
        self._transport_ctx = None
        self._session_ctx = None
        self._session = None

    async def connect(self) -> None:
        from velune.mcp.security import validate_mcp_url

        url = self.config.url
        if not url:
            raise MCPTransportError(
                f"HTTP server '{self.config.name}' has no 'url' configured."
            )

        validate_mcp_url(url, self._allowed_hosts)

        # Try the SDK's streamable HTTP transport first (available in mcp >= 1.6)
        try:
            await self._connect_via_sdk(url)
            return
        except (ImportError, AttributeError):
            pass

        # Fall back to manual HTTP session
        await self._connect_via_http(url)

    async def _connect_via_sdk(self, url: str) -> None:
        """Use mcp.client.streamable_http if available."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore[import]

        kwargs: dict[str, Any] = {"url": url}
        if self.config.headers:
            kwargs["headers"] = self.config.headers

        self._transport_ctx = streamablehttp_client(**kwargs)
        read, write, _ = await self._transport_ctx.__aenter__()

        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        logger.info("HTTP MCP server '%s' connected (SDK transport).", self.config.name)

    async def _connect_via_http(self, url: str) -> None:
        """Fallback: plain HTTP/JSON-RPC using httpx."""
        try:
            import httpx
        except ImportError:
            raise MCPTransportError(
                "HTTP transport requires 'httpx'. Install it with: pip install httpx"
            )
        self._http_client = httpx.AsyncClient(
            headers={"Content-Type": "application/json", **self.config.headers},
            timeout=self.config.timeout,
        )
        self._http_url = url
        self._session = None  # signals "manual mode"
        self._tools_cache: list[ToolInfo] = []
        # Discover tools via initialize → list_tools RPC
        try:
            await self._rpc("initialize", {"protocolVersion": "2024-11-05", "clientInfo": {"name": "velune", "version": "1.0"}, "capabilities": {}})
            tools_resp = await self._rpc("tools/list", {})
            raw_tools = tools_resp.get("tools", [])
            self._tools_cache = [
                ToolInfo(
                    name=t["name"],
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                    server_name=self.config.name,
                )
                for t in raw_tools
            ]
        except Exception as exc:
            await self._http_client.aclose()
            raise MCPTransportError(
                f"HTTP MCP handshake failed for '{self.config.name}': {exc}"
            ) from exc
        logger.info("HTTP MCP server '%s' connected (httpx fallback).", self.config.name)

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = await self._http_client.post(self._http_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise MCPTransportError(f"RPC error: {data['error']}")
        return data.get("result", {})

    async def disconnect(self) -> None:
        for ctx in (self._session_ctx, self._transport_ctx):
            if ctx is not None:
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
        # Close httpx client if we used the fallback
        http_client = getattr(self, "_http_client", None)
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:
                pass
        self._session = None
        self._session_ctx = None
        self._transport_ctx = None

    async def list_tools(self) -> list[ToolInfo]:
        # SDK session path
        if self._session is not None:
            try:
                result = await self._session.list_tools()
                return [
                    ToolInfo(
                        name=t.name,
                        description=t.description or "",
                        input_schema=t.inputSchema or {},
                        server_name=self.config.name,
                    )
                    for t in result.tools
                ]
            except Exception as exc:
                raise MCPTransportError(f"list_tools failed: {exc}") from exc
        # httpx fallback
        return getattr(self, "_tools_cache", [])

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self._session is not None:
            try:
                result = await self._session.call_tool(tool_name, arguments=arguments)
                return _extract_text(result)
            except Exception as exc:
                raise MCPTransportError(f"call_tool '{tool_name}' failed: {exc}") from exc
        # httpx fallback
        try:
            resp = await self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
            parts = [c["text"] for c in resp.get("content", []) if "text" in c]
            return "\n".join(parts) or str(resp)
        except Exception as exc:
            raise MCPTransportError(f"call_tool '{tool_name}' failed: {exc}") from exc


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
