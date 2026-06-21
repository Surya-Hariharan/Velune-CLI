"""Model Context Protocol (MCP) support for Velune.

Submodules are imported lazily (PEP 562) to avoid import cycles.

Quick start — single server::

    from velune.mcp import VeluneMCPClient
    client = VeluneMCPClient("http://localhost:7788/sse", "myserver")
    tools = await client.connect()

Quick start — multi-server registry (preferred)::

    from velune.mcp import MCPServerRegistry
    registry = MCPServerRegistry(workspace=Path("."))
    registry.load_config()           # reads .mcp.json
    await registry.connect_all()
    tools = registry.all_tools()     # flat list across all servers
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.mcp.client import VeluneMCPClient
    from velune.mcp.config import load_mcp_servers
    from velune.mcp.registry import MCPServerRegistry
    from velune.mcp.server import VeluneMCPServer

__all__ = [
    "VeluneMCPServer",
    "VeluneMCPClient",
    "MCPServerRegistry",
    "load_mcp_servers",
]


def __getattr__(name: str) -> Any:
    if name == "VeluneMCPClient":
        from velune.mcp.client import VeluneMCPClient

        return VeluneMCPClient
    if name == "MCPServerRegistry":
        from velune.mcp.registry import MCPServerRegistry

        return MCPServerRegistry
    if name == "load_mcp_servers":
        from velune.mcp.config import load_mcp_servers

        return load_mcp_servers
    if name == "VeluneMCPServer":
        from velune.mcp.server import VeluneMCPServer

        return VeluneMCPServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
