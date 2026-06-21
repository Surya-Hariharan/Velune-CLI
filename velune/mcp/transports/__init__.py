"""MCP transport layer — connect to external servers via stdio, SSE, or HTTP.

Usage::

    from velune.mcp.transports import connect_transport, TransportType, ServerConfig

    cfg = ServerConfig(
        name="filesystem",
        transport=TransportType.STDIO,
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )
    async with connect_transport(cfg) as conn:
        tools = await conn.list_tools()
        result = await conn.call_tool("read_file", {"path": "/tmp/foo.txt"})
"""

from velune.mcp.transports.base import (
    MCPConnection,
    MCPTransportError,
    ResourceInfo,
    ServerConfig,
    ToolInfo,
    TransportType,
)
from velune.mcp.transports.factory import connect_transport, make_connection

__all__ = [
    "connect_transport",
    "make_connection",
    "MCPConnection",
    "MCPTransportError",
    "ServerConfig",
    "TransportType",
    "ToolInfo",
    "ResourceInfo",
]
