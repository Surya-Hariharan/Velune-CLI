"""Transport factory — create the right MCPConnection for a ServerConfig."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from velune.mcp.transports.base import (
    MCPConnection,
    MCPTransportError,
    ServerConfig,
    TransportType,
)


def make_connection(
    config: ServerConfig,
    allowed_hosts: list[str] | None = None,
) -> MCPConnection:
    """Instantiate (but don't connect) the right MCPConnection for *config*.

    Args:
        config:        Server configuration, including transport type.
        allowed_hosts: Optional SSRF allowlist passed to SSE/HTTP transports.

    Returns:
        An unconnected ``MCPConnection`` subclass instance.
    """
    if config.transport == TransportType.STDIO:
        from velune.mcp.transports.stdio import StdioConnection
        return StdioConnection(config)

    if config.transport == TransportType.SSE:
        from velune.mcp.transports.sse import SSEConnection
        return SSEConnection(config, allowed_hosts=allowed_hosts)

    if config.transport == TransportType.HTTP:
        from velune.mcp.transports.http import HTTPConnection
        return HTTPConnection(config, allowed_hosts=allowed_hosts)

    raise MCPTransportError(
        f"Unsupported transport '{config.transport}' for server '{config.name}'. "
        "Supported: stdio, sse, http."
    )


@asynccontextmanager
async def connect_transport(
    config: ServerConfig,
    allowed_hosts: list[str] | None = None,
) -> AsyncIterator[MCPConnection]:
    """Async context manager: connect and yield an MCPConnection, then disconnect.

    Usage::

        async with connect_transport(cfg) as conn:
            tools = await conn.list_tools()
    """
    conn = make_connection(config, allowed_hosts=allowed_hosts)
    await conn.connect()
    try:
        yield conn
    finally:
        await conn.disconnect()
