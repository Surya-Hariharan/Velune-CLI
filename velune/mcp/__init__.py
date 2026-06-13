"""Model Context Protocol (MCP) support for Velune.

Submodules are imported lazily (PEP 562). Eagerly importing ``server`` here
pulls in the repository→cognition→CLI chain, which forms an import cycle when
anything under ``velune.mcp`` is imported before the CLI package finishes
loading. Lazy access keeps the public API (``from velune.mcp import ...``) while
letting lightweight submodules like :mod:`velune.mcp.security` be imported in
isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.mcp.client import VeluneMCPClient
    from velune.mcp.config import load_mcp_servers
    from velune.mcp.server import VeluneMCPServer

__all__ = ["VeluneMCPServer", "VeluneMCPClient", "load_mcp_servers"]


def __getattr__(name: str) -> Any:
    if name == "VeluneMCPClient":
        from velune.mcp.client import VeluneMCPClient

        return VeluneMCPClient
    if name == "load_mcp_servers":
        from velune.mcp.config import load_mcp_servers

        return load_mcp_servers
    if name == "VeluneMCPServer":
        from velune.mcp.server import VeluneMCPServer

        return VeluneMCPServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
