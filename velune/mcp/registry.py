"""MCPServerRegistry — manages multiple concurrent MCP server connections.

Responsibilities:
- Load server configs from ``.mcp.json`` and ``velune.toml``
- Connect / disconnect individual or all servers
- Maintain per-server health state (connected / error / disconnected)
- Expose a flat view of all tools across all servers for the model
- Cache tool lists until explicitly invalidated

Usage::

    registry = MCPServerRegistry(workspace=Path("."))
    await registry.connect_all()

    # Get all tools available to the model
    tools = registry.all_tools()

    # Call a tool (registry resolves which server owns it)
    result = await registry.call_tool("filesystem_read_file", {"path": "/tmp/foo"})

    await registry.disconnect_all()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune._compat import StrEnum
from velune.mcp.transports.base import (
    MCPConnection,
    MCPTransportError,
    ResourceInfo,
    ServerConfig,
    ToolInfo,
)
from velune.mcp.transports.factory import make_connection

logger = logging.getLogger("velune.mcp.registry")


class ServerState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class ServerEntry:
    """Runtime state for one managed MCP server."""

    config: ServerConfig
    state: ServerState = ServerState.DISCONNECTED
    connection: MCPConnection | None = None
    tools: list[ToolInfo] = field(default_factory=list)
    resources: list[ResourceInfo] = field(default_factory=list)
    error: str = ""

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_connected(self) -> bool:
        return self.state == ServerState.CONNECTED


class MCPServerRegistry:
    """Manages multiple MCP server connections and exposes a unified tool surface.

    The registry is the single source of truth for MCP state during a REPL
    session. Create one per session; wire it into the REPL so it can inject
    MCP tools into the model's context.
    """

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self._allowed_hosts = allowed_hosts
        self._entries: dict[str, ServerEntry] = {}
        self._tool_to_server: dict[str, str] = {}  # qualified tool name → server name
        self._trusted: bool = True
        self._watched_mtimes: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Configuration loading
    # ------------------------------------------------------------------

    def load_config(
        self,
        extra_configs: list[ServerConfig] | None = None,
        *,
        trusted: bool = True,
    ) -> None:
        """Load server configs from ``.mcp.json`` (and optionally extras).

        Merges: ``.mcp.json`` in workspace > user home ``~/.mcp.json`` > extras.
        Already-registered server names are updated (not duplicated).

        When *trusted* is False the workspace is treated as untrusted: project-
        level sources (``<workspace>/.mcp.json`` and ``velune.toml
        [mcp.servers]``) are skipped entirely so a cloned/downloaded repository
        cannot auto-spawn MCP servers. User-level (``~/.mcp.json``) and
        caller-provided configs are always honored.
        """
        self._trusted = trusted
        configs: list[ServerConfig] = []

        # User-level .mcp.json is always trusted. The project-level file is only
        # loaded when the workspace has been explicitly trusted.
        mcp_json_paths = [Path.home() / ".mcp.json"]
        if trusted:
            mcp_json_paths.insert(0, self.workspace / ".mcp.json")
        for path in mcp_json_paths:
            configs.extend(_load_mcp_json(path))

        # velune.toml [mcp.servers] entries are project-controlled — trusted only.
        if trusted:
            configs.extend(_load_toml_mcp(self.workspace))
        elif (self.workspace / ".mcp.json").exists() or (self.workspace / "velune.toml").exists():
            logger.info(
                "Skipping project-level MCP config in untrusted workspace %s; "
                "run 'velune trust' to enable it.",
                self.workspace,
            )

        # Caller-provided extras (e.g. from CLI --mcp-server flag)
        if extra_configs:
            configs.extend(extra_configs)

        for cfg in configs:
            if cfg.name not in self._entries:
                self._entries[cfg.name] = ServerEntry(config=cfg)
            else:
                # Update config in-place if already registered
                self._entries[cfg.name].config = cfg

        logger.debug("MCPServerRegistry loaded %d server config(s).", len(self._entries))

    def register(self, config: ServerConfig) -> None:
        """Manually register a server config (e.g. from CLI flags)."""
        if config.name in self._entries and self._entries[config.name].is_connected:
            logger.debug("Server '%s' already connected; skipping re-register.", config.name)
            return
        self._entries[config.name] = ServerEntry(config=config)

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self, name: str) -> bool:
        """Connect a single server by name. Returns True on success."""
        entry = self._entries.get(name)
        if entry is None:
            logger.warning("connect: unknown server '%s'", name)
            return False
        if entry.is_connected:
            return True

        entry.state = ServerState.CONNECTING
        entry.error = ""
        conn = make_connection(entry.config, allowed_hosts=self._allowed_hosts)
        try:
            await asyncio.wait_for(conn.connect(), timeout=15.0)
            entry.connection = conn
            entry.state = ServerState.CONNECTED
            # Discover tools and resources
            entry.tools = await asyncio.wait_for(conn.list_tools(), timeout=10.0)
            try:
                entry.resources = await asyncio.wait_for(conn.list_resources(), timeout=10.0)
            except Exception:
                entry.resources = []
            self._rebuild_tool_index()
            logger.info("MCP server '%s' connected (%d tools).", name, len(entry.tools))
            return True
        except MCPTransportError as exc:
            entry.state = ServerState.ERROR
            entry.error = str(exc)
            logger.error("MCP server '%s' failed to connect: %s", name, exc)
            return False
        except Exception as exc:
            entry.state = ServerState.ERROR
            entry.error = str(exc)
            logger.error("Unexpected error connecting to '%s': %s", name, exc)
            return False

    async def disconnect(self, name: str) -> None:
        """Disconnect a single server by name."""
        entry = self._entries.get(name)
        if entry is None or entry.connection is None:
            return
        try:
            await entry.connection.disconnect()
        except Exception as exc:
            logger.debug("Error disconnecting '%s': %s", name, exc)
        entry.connection = None
        entry.state = ServerState.DISCONNECTED
        entry.tools = []
        entry.resources = []
        self._rebuild_tool_index()

    async def connect_all(self, timeout: float = 30.0) -> dict[str, bool]:
        """Connect all registered servers concurrently.

        Returns a dict of ``{name: success}``.
        """
        if not self._entries:
            return {}

        results = await asyncio.gather(
            *[self.connect(name) for name in self._entries],
            return_exceptions=True,
        )
        return {
            name: (result is True) for name, result in zip(self._entries, results, strict=False)
        }

    async def disconnect_all(self) -> None:
        """Disconnect all servers concurrently."""
        await asyncio.gather(
            *[self.disconnect(name) for name in list(self._entries)],
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Tool access
    # ------------------------------------------------------------------

    def all_tools(self) -> list[ToolInfo]:
        """Return all tools across all connected servers."""
        tools: list[ToolInfo] = []
        for entry in self._entries.values():
            if entry.is_connected:
                tools.extend(entry.tools)
        return tools

    def all_resources(self) -> list[ResourceInfo]:
        """Return all resources across all connected servers."""
        resources: list[ResourceInfo] = []
        for entry in self._entries.values():
            if entry.is_connected:
                resources.extend(entry.resources)
        return resources

    async def call_tool(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool by its qualified name (as returned by ``all_tools()``).

        The registry uses an internal index to route the call to the correct
        server without the caller needing to know which server owns the tool.
        """
        # Direct lookup via qualified name
        server_name = self._tool_to_server.get(qualified_name)
        if server_name is None:
            # Try stripping the server prefix (e.g. "filesystem_read_file" → "read_file")
            for entry in self._entries.values():
                if entry.is_connected:
                    for tool in entry.tools:
                        if tool.name == qualified_name:
                            server_name = entry.name
                            break
                if server_name:
                    break

        if server_name is None:
            raise MCPTransportError(f"No MCP server owns tool '{qualified_name}'.")

        entry = self._entries[server_name]
        if not entry.is_connected or entry.connection is None:
            raise MCPTransportError(f"MCP server '{server_name}' is not connected.")

        # Unqualify the tool name before passing to the server
        raw_name = _strip_server_prefix(qualified_name, server_name)
        try:
            return await asyncio.wait_for(
                entry.connection.call_tool(raw_name, arguments), timeout=60.0
            )
        except asyncio.TimeoutError:
            entry.state = ServerState.ERROR
            entry.error = "Tool execution timed out."
            raise MCPTransportError(
                f"MCP tool '{qualified_name}' on '{server_name}' timed out after 60 seconds."
            )
        except Exception as exc:
            # Reconnect might be needed
            entry.state = ServerState.ERROR
            entry.error = str(exc)
            raise MCPTransportError(f"MCP tool '{qualified_name}' execution failed: {exc}")

    async def read_resource(self, uri: str) -> str:
        """Read a resource from whichever connected server exposes it."""
        for entry in self._entries.values():
            if not entry.is_connected or entry.connection is None:
                continue
            for res in entry.resources:
                if res.uri == uri:
                    return await entry.connection.read_resource(uri)
        raise MCPTransportError(f"No connected MCP server has resource '{uri}'.")

    # ------------------------------------------------------------------
    # Introspection (for /mcp command)
    # ------------------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """Return a list of dicts summarising all registered servers."""
        rows = []
        for entry in self._entries.values():
            rows.append(
                {
                    "name": entry.name,
                    "state": entry.state.value,
                    "transport": entry.config.transport.value,
                    "endpoint": entry.config.url
                    or f"{entry.config.command} {' '.join(entry.config.args)}",
                    "tools": len(entry.tools),
                    "resources": len(entry.resources),
                    "error": entry.error,
                }
            )
        return rows

    def tools_for_server(self, name: str) -> list[ToolInfo]:
        """Return all tools for a specific server."""
        entry = self._entries.get(name)
        return entry.tools if entry else []

    def resources_for_server(self, name: str) -> list[ResourceInfo]:
        """Return all resources for a specific server."""
        entry = self._entries.get(name)
        return entry.resources if entry else []

    async def refresh_tools(self, name: str) -> bool:
        """Re-fetch the tool list from a connected server (after hot-reload)."""
        entry = self._entries.get(name)
        if entry is None or not entry.is_connected or entry.connection is None:
            return False
        try:
            entry.tools = await entry.connection.list_tools()
            entry.resources = await entry.connection.list_resources()
            self._rebuild_tool_index()
            return True
        except Exception as exc:
            logger.warning("refresh_tools failed for '%s': %s", name, exc)
            return False

    # ------------------------------------------------------------------
    # Environment-based discovery
    # ------------------------------------------------------------------

    def load_env(self) -> int:
        """Load MCP server configs from the ``MCP_SERVERS_JSON`` environment variable.

        Format: JSON object mapping server name → server config dict.
        Example::

            MCP_SERVERS_JSON='{"fs": {"command": "uvx", "args": ["mcp-server-filesystem", "/tmp"]}}'

        Returns the number of servers successfully loaded.
        """
        import json
        import os

        raw = os.environ.get("MCP_SERVERS_JSON", "").strip()
        if not raw:
            return 0
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("MCP_SERVERS_JSON is not valid JSON: %s", exc)
            return 0
        if not isinstance(data, dict):
            logger.warning(
                "MCP_SERVERS_JSON must be a JSON object, got %s", type(data).__name__
            )
            return 0
        count = 0
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                cfg = ServerConfig.from_dict(name, entry)
                if name not in self._entries:
                    self._entries[name] = ServerEntry(config=cfg)
                count += 1
            except Exception as exc:
                logger.warning(
                    "Skipping malformed MCP_SERVERS_JSON entry '%s': %s", name, exc
                )
        if count:
            logger.info("MCP_SERVERS_JSON: loaded %d server(s).", count)
        return count

    # ------------------------------------------------------------------
    # Hot-reload / config watching
    # ------------------------------------------------------------------

    async def watch(self, interval_secs: float = 30.0) -> None:
        """Poll config files for changes and hot-reload servers when they differ.

        Watches ``<workspace>/.mcp.json`` and ``<workspace>/velune.toml``.
        Designed to run as a background ``asyncio.Task`` for the lifetime of the
        REPL session.
        """
        watch_paths = [
            self.workspace / ".mcp.json",
            self.workspace / "velune.toml",
        ]
        self._watched_mtimes = _read_mtimes(watch_paths)
        while True:
            try:
                await asyncio.sleep(interval_secs)
            except asyncio.CancelledError:
                return
            try:
                current = _read_mtimes(watch_paths)
                if current != self._watched_mtimes:
                    self._watched_mtimes = current
                    logger.info("MCP config change detected — hot-reloading servers.")
                    await self._hot_reload()
            except Exception as exc:
                logger.debug("MCP watch error (non-fatal): %s", exc)

    async def _hot_reload(self) -> None:
        """Diff current vs reloaded config; disconnect removed servers, connect new ones."""
        old_names = set(self._entries)
        self.load_config(trusted=self._trusted)
        self.load_env()
        new_names = set(self._entries)

        removed = old_names - new_names
        added = new_names - old_names

        for name in removed:
            await self.disconnect(name)
            self._entries.pop(name, None)
            logger.info("MCP hot-reload: removed server '%s'.", name)

        for name in added:
            success = await self.connect(name)
            logger.info(
                "MCP hot-reload: added server '%s' (%s).",
                name,
                "connected" if success else "failed",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rebuild_tool_index(self) -> None:
        """Rebuild the qualified-name → server-name lookup table."""
        self._tool_to_server.clear()
        for entry in self._entries.values():
            if not entry.is_connected:
                continue
            for tool in entry.tools:
                # Qualified name: "{server_name}_{tool_name}"
                qualified = f"{entry.name}_{tool.name}"
                self._tool_to_server[qualified] = entry.name
                # Also index the raw name so both forms work
                self._tool_to_server[tool.name] = entry.name


# ---------------------------------------------------------------------------
# Config loaders (private)
# ---------------------------------------------------------------------------


def _load_mcp_json(path: Path) -> list[ServerConfig]:
    """Load server configs from a ``.mcp.json`` file."""
    if not path.exists():
        return []
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        configs = []
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                configs.append(ServerConfig.from_dict(name, entry))
            except Exception as exc:
                logger.warning("Skipping malformed .mcp.json entry '%s': %s", name, exc)
        return configs
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return []


def _load_toml_mcp(workspace: Path) -> list[ServerConfig]:
    """Load MCP server entries from velune.toml ``[mcp.servers]`` section."""
    toml_path = workspace / "velune.toml"
    if not toml_path.exists():
        return []
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        servers = data.get("mcp", {}).get("servers", {})
        if not isinstance(servers, dict):
            return []
        configs = []
        for name, entry in servers.items():
            if isinstance(entry, str):
                # Simple URL shorthand: servers.myserver = "https://..."
                configs.append(ServerConfig(name=name, url=entry))
            elif isinstance(entry, dict):
                try:
                    configs.append(ServerConfig.from_dict(name, entry))
                except Exception as exc:
                    logger.warning("Skipping malformed velune.toml mcp entry '%s': %s", name, exc)
        return configs
    except Exception as exc:
        logger.debug("Could not load velune.toml mcp section: %s", exc)
        return []


def _strip_server_prefix(qualified: str, server_name: str) -> str:
    """Remove ``{server_name}_`` prefix from a qualified tool name."""
    prefix = f"{server_name}_"
    if qualified.startswith(prefix):
        return qualified[len(prefix) :]
    return qualified


def _read_mtimes(paths: list[Path]) -> dict[str, float]:
    """Return a dict of path → mtime for each path that exists."""
    result: dict[str, float] = {}
    for p in paths:
        try:
            result[str(p)] = p.stat().st_mtime
        except FileNotFoundError:
            result[str(p)] = 0.0
    return result
