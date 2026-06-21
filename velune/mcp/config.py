"""Configuration utility for loading external MCP servers."""

from __future__ import annotations

from pathlib import Path

from velune.kernel.config import ConfigLoader


def load_mcp_servers(config_path: Path | None = None) -> dict[str, str]:
    """Load external MCP server configurations from velune.toml."""
    try:
        loader = ConfigLoader(config_path)
        config = loader.load()
        if hasattr(config, "mcp") and config.mcp:
            return config.mcp.servers
    except Exception:
        pass
    return {}
