"""WebSocket transport for MCP servers accessible via ws:// or wss://.

Connects to an MCP server over a persistent WebSocket, sends JSON-RPC 2.0
messages, and returns structured results.

Example config (in ``.mcp.json``)::

    {
      "my-ws-server": {
        "type": "ws",
        "url": "ws://localhost:8765/mcp",
        "headers": {
          "Authorization": "Bearer ${API_TOKEN}"
        }
      }
    }

Dependencies:
    ``websockets`` is required (``pip install websockets``).  The transport
    degrades to :class:`MCPTransportError` at connect time if the package is
    absent, so it does not affect startup when unused.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

from velune.mcp.transports.base import (
    MCPConnection,
    MCPTransportError,
    ResourceInfo,
    ServerConfig,
    ToolInfo,
)

logger = logging.getLogger("velune.mcp.transports.websocket")

# Shared counter for JSON-RPC message IDs within a connection lifetime.
_id_counter = itertools.count(1)


class WebSocketConnection(MCPConnection):
    """Live connection to a WebSocket-based MCP server.

    Implements the :class:`MCPConnection` contract over a JSON-RPC 2.0 stream
    carried on a persistent WebSocket.  Both ``ws://`` and ``wss://`` URLs are
    supported; the latter enables TLS.

    Lifecycle::

        conn = WebSocketConnection(config)
        await conn.connect()      # opens WS, sends initialize
        tools = await conn.list_tools()
        result = await conn.call_tool("echo", {"text": "hello"})
        await conn.disconnect()

    Or use the inherited context-manager::

        async with conn.session():
            ...
    """

    def __init__(
        self,
        config: ServerConfig,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(config)
        self._allowed_hosts = allowed_hosts
        self._ws: Any = None
        self._tools_cache: list[ToolInfo] = []
        self._resources_cache: list[ResourceInfo] = []

    # ── Connection lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open a WebSocket, validate the URL, and run MCP ``initialize``."""
        from velune.mcp.security import validate_mcp_url

        url = self.config.url
        if not url:
            raise MCPTransportError(
                f"WebSocket server '{self.config.name}' has no 'url' configured."
            )

        # Normalise: validate_mcp_url works on http/https; convert for SSRF check.
        check_url = url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        validate_mcp_url(check_url, self._allowed_hosts)

        if not url.startswith(("ws://", "wss://")):
            raise MCPTransportError(
                f"WebSocket server '{self.config.name}' URL must start with ws:// or wss://, "
                f"got: {url!r}"
            )

        try:
            import websockets  # type: ignore[import]
        except ImportError:
            raise MCPTransportError(
                "WebSocket transport requires 'websockets'. Install it with: pip install websockets"
            )

        extra_headers = dict(self.config.headers) if self.config.headers else {}
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers=extra_headers),
                timeout=self.config.timeout,
            )
        except TimeoutError:
            raise MCPTransportError(
                f"WebSocket connection to '{self.config.name}' ({url}) timed out "
                f"after {self.config.timeout}s."
            )
        except Exception as exc:
            raise MCPTransportError(
                f"WebSocket connection to '{self.config.name}' ({url}) failed: {exc}"
            ) from exc

        # MCP handshake
        try:
            await self._initialize()
            await self._discover_tools()
        except Exception as exc:
            await self.disconnect()
            raise MCPTransportError(
                f"MCP handshake failed for WebSocket server '{self.config.name}': {exc}"
            ) from exc

        logger.info("WebSocket MCP server '%s' connected at %s.", self.config.name, url)

    async def disconnect(self) -> None:
        """Close the WebSocket gracefully."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.debug("WebSocket MCP server '%s' disconnected.", self.config.name)

    # ── MCP protocol ─────────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolInfo]:
        """Return cached tool list (populated at connect time)."""
        return list(self._tools_cache)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Send a ``tools/call`` RPC and return the text content of the response.

        Args:
            tool_name: Name of the tool to invoke.
            arguments: Tool input parameters.

        Returns:
            Concatenated text from the response content array.

        Raises:
            MCPTransportError: If the call fails or the server returns an error.
        """
        if self._ws is None:
            raise MCPTransportError(f"WebSocket server '{self.config.name}' is not connected.")
        try:
            result = await self._rpc(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
            parts = [c["text"] for c in result.get("content", []) if "text" in c]
            return "\n".join(parts) or json.dumps(result)
        except MCPTransportError:
            raise
        except Exception as exc:
            raise MCPTransportError(
                f"call_tool '{tool_name}' on '{self.config.name}' failed: {exc}"
            ) from exc

    async def list_resources(self) -> list[ResourceInfo]:
        """Return cached resources (populated at connect time if server supports them)."""
        return list(self._resources_cache)

    async def read_resource(self, uri: str) -> str:
        """Read a resource by URI via JSON-RPC.

        Args:
            uri: Resource URI as returned by :meth:`list_resources`.

        Returns:
            Resource content as a string.

        Raises:
            MCPTransportError: If the server returns an error or is disconnected.
        """
        if self._ws is None:
            raise MCPTransportError(f"WebSocket server '{self.config.name}' is not connected.")
        try:
            result = await self._rpc("resources/read", {"uri": uri})
            contents = result.get("contents", [])
            parts = [c.get("text", "") for c in contents if "text" in c]
            return "\n".join(parts) or json.dumps(result)
        except MCPTransportError:
            raise
        except Exception as exc:
            raise MCPTransportError(
                f"read_resource '{uri}' on '{self.config.name}' failed: {exc}"
            ) from exc

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _initialize(self) -> None:
        """Send MCP ``initialize`` and consume ``initialized`` notification."""
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "velune", "version": "1.0"},
                "capabilities": {},
            },
        )

    async def _discover_tools(self) -> None:
        """Populate ``_tools_cache`` from ``tools/list``."""
        result = await self._rpc("tools/list", {})
        self._tools_cache = [
            ToolInfo(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.config.name,
            )
            for t in result.get("tools", [])
        ]

        # Optionally discover resources (not all servers support this)
        try:
            res_result = await self._rpc("resources/list", {})
            self._resources_cache = [
                ResourceInfo(
                    uri=r["uri"],
                    name=r.get("name", r["uri"]),
                    description=r.get("description", ""),
                    mime_type=r.get("mimeType", ""),
                    server_name=self.config.name,
                )
                for r in res_result.get("resources", [])
            ]
        except MCPTransportError:
            pass

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request and return the ``result`` dict.

        Args:
            method: JSON-RPC method name.
            params: Method parameters.

        Returns:
            The ``result`` field of the JSON-RPC response.

        Raises:
            MCPTransportError: On network error, timeout, or RPC-level error.
        """
        if self._ws is None:
            raise MCPTransportError(
                f"Cannot call '{method}': WebSocket for '{self.config.name}' is not open."
            )

        msg_id = next(_id_counter)
        payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})

        try:
            await asyncio.wait_for(
                self._ws.send(payload),
                timeout=self.config.timeout,
            )
            raw = await asyncio.wait_for(
                self._ws.recv(),
                timeout=self.config.timeout,
            )
        except TimeoutError:
            raise MCPTransportError(
                f"WebSocket RPC '{method}' on '{self.config.name}' timed out "
                f"after {self.config.timeout}s."
            )
        except Exception as exc:
            raise MCPTransportError(
                f"WebSocket RPC '{method}' on '{self.config.name}' failed: {exc}"
            ) from exc

        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPTransportError(
                f"WebSocket server '{self.config.name}' returned non-JSON: {raw!r}"
            ) from exc

        if "error" in data:
            err = data["error"]
            raise MCPTransportError(
                f"MCP error from '{self.config.name}' method='{method}': "
                f"[{err.get('code', '?')}] {err.get('message', err)}"
            )

        return data.get("result", {})
