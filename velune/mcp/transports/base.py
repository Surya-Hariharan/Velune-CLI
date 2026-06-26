"""Common types and abstract base for all MCP transports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from velune._compat import StrEnum


class TransportType(StrEnum):
    """Wire protocol for an MCP server connection."""

    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"
    WEBSOCKET = "ws"


@dataclass
class ServerConfig:
    """Unified config for any MCP server regardless of transport.

    Maps directly to the shape of entries in ``.mcp.json``.
    """

    name: str
    transport: TransportType = TransportType.SSE

    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)

    # SSE / HTTP / WebSocket fields
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    # Common
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 30

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> ServerConfig:
        """Build from a raw ``.mcp.json`` entry dict."""
        raw_type = str(d.get("type", "")).lower()
        if raw_type in ("sse", "http", "ws", "websocket"):
            transport = TransportType(raw_type if raw_type != "websocket" else "ws")
        elif "command" in d:
            transport = TransportType.STDIO
        else:
            transport = TransportType.SSE

        return cls(
            name=name,
            transport=transport,
            command=d.get("command", ""),
            args=list(d.get("args", [])),
            url=d.get("url", ""),
            headers=dict(d.get("headers", {})),
            env=dict(d.get("env", {})),
            timeout=int(d.get("timeout", 30)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to the ``.mcp.json`` entry shape."""
        d: dict[str, Any] = {}
        if self.transport == TransportType.STDIO:
            d["command"] = self.command
            if self.args:
                d["args"] = self.args
        else:
            d["type"] = str(self.transport)
            d["url"] = self.url
            if self.headers:
                d["headers"] = self.headers
        if self.env:
            d["env"] = self.env
        if self.timeout != 30:
            d["timeout"] = self.timeout
        return d


@dataclass
class ToolInfo:
    """Metadata about a tool exposed by an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str = ""


@dataclass
class ResourceInfo:
    """Metadata about a resource (file/data) exposed by an MCP server."""

    uri: str
    name: str
    description: str = ""
    mime_type: str = ""
    server_name: str = ""


class MCPTransportError(RuntimeError):
    """Raised when an MCP transport fails to connect or execute."""


class MCPConnection(ABC):
    """Abstract live connection to an MCP server.

    Implementations wrap the MCP Python SDK's session object for a specific
    transport type.  All methods are async; callers must ``await`` them.
    """

    def __init__(self, config: ServerConfig) -> None:
        self.config = config

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection and initialise the MCP session."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the connection and free resources."""

    @abstractmethod
    async def list_tools(self) -> list[ToolInfo]:
        """Return all tools offered by the server."""

    @abstractmethod
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool and return its text output."""

    async def list_resources(self) -> list[ResourceInfo]:
        """Return all resources offered by the server (optional capability)."""
        return []

    async def read_resource(self, uri: str) -> str:
        """Read the content of a resource by URI (optional capability)."""
        raise NotImplementedError(f"{type(self).__name__} does not support resources")

    @property
    def server_name(self) -> str:
        return self.config.name

    @asynccontextmanager
    async def session(self) -> AsyncIterator[MCPConnection]:
        """Context manager: connect on enter, disconnect on exit."""
        await self.connect()
        try:
            yield self
        finally:
            await self.disconnect()
