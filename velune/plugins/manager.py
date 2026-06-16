"""PluginManager — orchestrates declarative plugin lifecycle.

Responsibilities:
- Scan for plugins using PluginScanner
- Register plugin slash commands into the REPL's SlashCommandRegistry
- Inject plugin hooks into the session HookDispatcher
- Register plugin MCP servers into the MCPServerRegistry
- Expose skills for context injection
- Handle enable/disable/reload at runtime

Usage (from REPL)::

    manager = PluginManager(workspace=Path("."))
    manager.load()

    # Access all available plugin commands
    for cmd in manager.all_commands():
        registry.register(SlashCommand(name=cmd.name, ...))

    # Get skills matching a user message
    context_blocks = manager.matching_skills("can you do a code review?")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune.plugins.declarative.scanner import DiscoveredPlugin, PluginScanner
from velune.plugins.declarative.command import PluginCommand
from velune.plugins.declarative.skill import PluginSkill

if TYPE_CHECKING:
    from velune.hooks import HookDispatcher
    from velune.mcp.registry import MCPServerRegistry

logger = logging.getLogger("velune.plugins.manager")


class PluginManager:
    """Coordinates plugin discovery, registration, and runtime management.

    Create one per session, call ``load()`` after the REPL is ready.
    """

    def __init__(
        self,
        workspace: Path | None = None,
        extra_search_paths: list[Path] | None = None,
    ) -> None:
        self.workspace = workspace
        self._scanner = PluginScanner.default_paths(workspace)
        if extra_search_paths:
            for p in extra_search_paths:
                self._scanner.add_path(p)

        self._plugins: dict[str, DiscoveredPlugin] = {}   # name → plugin
        self._disabled: set[str] = set()                  # names disabled at runtime

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def load(self) -> list[DiscoveredPlugin]:
        """Scan all search paths and load plugins.  Returns newly found plugins."""
        discovered = self._scanner.scan()
        new: list[DiscoveredPlugin] = []
        for plugin in discovered:
            if plugin.name not in self._plugins:
                self._plugins[plugin.name] = plugin
                new.append(plugin)
        logger.info("PluginManager: %d plugin(s) loaded total.", len(self._plugins))
        return new

    def reload(self, name: str | None = None) -> list[DiscoveredPlugin]:
        """Reload a specific plugin or all plugins from disk."""
        if name is not None:
            if name in self._plugins:
                del self._plugins[name]
        else:
            self._plugins.clear()
        return self.load()

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def enable(self, name: str) -> bool:
        self._disabled.discard(name)
        if name in self._plugins:
            self._plugins[name].manifest.enabled = True
            return True
        return False

    def disable(self, name: str) -> bool:
        self._disabled.add(name)
        if name in self._plugins:
            self._plugins[name].manifest.enabled = False
            return True
        return False

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled and (
            name in self._plugins and self._plugins[name].enabled
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def all_commands(self) -> list[PluginCommand]:
        """Return all commands from all enabled plugins."""
        cmds: list[PluginCommand] = []
        for plugin in self._active_plugins():
            cmds.extend(plugin.commands)
        return cmds

    def find_command(self, name: str) -> PluginCommand | None:
        """Find a plugin command by its name (or alias)."""
        for plugin in self._active_plugins():
            for cmd in plugin.commands:
                if cmd.name == name or name in cmd.aliases:
                    return cmd
        return None

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    def all_skills(self) -> list[PluginSkill]:
        """Return all skills from all enabled plugins."""
        skills: list[PluginSkill] = []
        for plugin in self._active_plugins():
            skills.extend(plugin.skills)
        return skills

    def matching_skills(self, user_text: str) -> list[str]:
        """Return context blocks for skills that match *user_text*."""
        return [s.context_block for s in self.all_skills() if s.matches(user_text)]

    # ------------------------------------------------------------------
    # Hook wiring
    # ------------------------------------------------------------------

    def wire_hooks(self, dispatcher: "HookDispatcher") -> int:
        """Load plugin hook configs into *dispatcher*.  Returns count wired."""
        count = 0
        for plugin in self._active_plugins():
            if not plugin.has_hooks:
                continue
            hooks_file = plugin.manifest.hooks_file
            if not hooks_file.exists():
                continue
            try:
                from velune.hooks.config import _parse_bindings
                import json

                data = json.loads(hooks_file.read_text(encoding="utf-8"))
                # Support both {hooks: {...}} wrapper and bare {EventName: [...]} format
                hooks_section = data.get("hooks", data)
                bindings = _parse_bindings(hooks_section)
                # Merge into dispatcher's binding cache
                existing = dispatcher._ensure_loaded()
                existing.extend(bindings)
                count += len(bindings)
                logger.info(
                    "Wired %d hook binding(s) from plugin '%s'.",
                    len(bindings),
                    plugin.name,
                )
            except Exception as exc:
                logger.warning("Failed to wire hooks from plugin '%s': %s", plugin.name, exc)
        return count

    # ------------------------------------------------------------------
    # MCP wiring
    # ------------------------------------------------------------------

    def wire_mcp(self, mcp_registry: "MCPServerRegistry") -> int:
        """Load plugin .mcp.json files into *mcp_registry*.  Returns count wired."""
        count = 0
        for plugin in self._active_plugins():
            if not plugin.has_mcp:
                continue
            mcp_file = plugin.manifest.mcp_file
            if not mcp_file.exists():
                continue
            try:
                import json
                from velune.mcp.transports.base import ServerConfig

                data = json.loads(mcp_file.read_text(encoding="utf-8"))
                for server_name, entry in data.items():
                    if not isinstance(entry, dict):
                        continue
                    # Namespace server name: plugin-name:server-name
                    qualified = f"{plugin.name}:{server_name}"
                    cfg = ServerConfig.from_dict(qualified, entry)
                    # Resolve ${VELUNE_PLUGIN_ROOT} in command/url
                    root_str = str(plugin.root)
                    cfg.command = cfg.command.replace("${VELUNE_PLUGIN_ROOT}", root_str)
                    cfg.command = cfg.command.replace("${CLAUDE_PLUGIN_ROOT}", root_str)
                    cfg.url = cfg.url.replace("${VELUNE_PLUGIN_ROOT}", root_str)
                    cfg.url = cfg.url.replace("${CLAUDE_PLUGIN_ROOT}", root_str)
                    cfg.args = [
                        a.replace("${VELUNE_PLUGIN_ROOT}", root_str)
                         .replace("${CLAUDE_PLUGIN_ROOT}", root_str)
                        for a in cfg.args
                    ]
                    mcp_registry.register(cfg)
                    count += 1
            except Exception as exc:
                logger.warning("Failed to wire MCP from plugin '%s': %s", plugin.name, exc)
        return count

    # ------------------------------------------------------------------
    # Introspection (for /plugin command)
    # ------------------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """Summary list for the /plugin display."""
        return [p.summary() for p in self._plugins.values()]

    def get_plugin(self, name: str) -> DiscoveredPlugin | None:
        return self._plugins.get(name)

    @property
    def plugin_count(self) -> int:
        return len(self._plugins)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _active_plugins(self) -> list[DiscoveredPlugin]:
        return [p for p in self._plugins.values() if p.enabled and p.name not in self._disabled]
