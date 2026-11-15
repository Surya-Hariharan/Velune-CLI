"""PluginManager — orchestrates declarative plugin lifecycle.

Responsibilities:
- Scan for plugins using PluginScanner
- Register plugin slash commands into the REPL's SlashCommandRegistry
- Inject plugin hooks into the session HookDispatcher
- Register plugin MCP servers into the MCPServerRegistry
- Expose skills for context injection
- Handle enable/disable/reload at runtime

Everything a plugin registers is tagged with the plugin's name (its *owner*) in the registry
that holds it. That is what makes the lifecycle real: wiring a plugin twice replaces rather
than duplicates, and :meth:`PluginManager.revoke` (used by disable, reload and a revoked
workspace trust) removes exactly what that plugin registered, from every registry it was
wired into, and nothing else.

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

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune.plugins.declarative.command import PluginCommand
from velune.plugins.declarative.paths import PluginPathError
from velune.plugins.declarative.scanner import (
    ORIGIN_EXTRA,
    ORIGIN_USER,
    ORIGIN_WORKSPACE,
    DiscoveredPlugin,
    PluginScanner,
)
from velune.plugins.declarative.skill import PluginSkill

if TYPE_CHECKING:
    from velune.cli.slash_commands import SlashCommandRegistry
    from velune.hooks import HookDispatcher
    from velune.hooks.types import HookBinding
    from velune.mcp.registry import MCPServerRegistry, ServerEntry
    from velune.mcp.transports.base import ServerConfig

logger = logging.getLogger("velune.plugins.manager")


def _validate_mcp_entry(server_name: str, entry: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless an ``.mcp.json`` entry has the field types it must have.

    The file is the plugin author's data. ``{"command": null}`` or ``"args": [1]`` would
    otherwise fail deep inside token substitution, or (``"args": "abc"``) be silently read as
    the list ``["a", "b", "c"]``. A plugin with an invalid server registers none of its servers.
    """
    for key in ("command", "url", "type"):
        if key in entry and not isinstance(entry[key], str):
            raise ValueError(f"server '{server_name}': '{key}' must be text")
    if "args" in entry and not (
        isinstance(entry["args"], list) and all(isinstance(a, str) for a in entry["args"])
    ):
        raise ValueError(f"server '{server_name}': 'args' must be a list of text")
    for key in ("env", "headers"):
        if key in entry and not isinstance(entry[key], dict):
            raise ValueError(f"server '{server_name}': '{key}' must be a mapping")
    if "headers" in entry and not all(
        isinstance(k, str) and isinstance(v, str) for k, v in entry["headers"].items()
    ):
        raise ValueError(f"server '{server_name}': 'headers' must map text to text")


class PluginManager:
    """Coordinates plugin discovery, registration, and runtime management.

    Create one per session, call ``load()`` after the REPL is ready.

    Workspace trust: plugins under ``<workspace>/.velune/plugins`` are project-controlled
    (their hooks and MCP servers run local commands), so they are only loaded, wired or
    exposed while the workspace is trusted. The manager starts *untrusted*; the session
    reports the user's decision with :meth:`set_workspace_trusted` before ``load()``.
    User-level plugins (``~/.velune/plugins``) are not affected.

    Lifecycle: the registries a plugin is wired into are remembered, so :meth:`disable`,
    :meth:`reload` and :meth:`set_workspace_trusted` can revoke everything the plugin
    registered without the caller having to know where it went. Closing the connections of
    revoked MCP servers is asynchronous: it is scheduled automatically when an event loop is
    running, and :meth:`flush` waits for it.
    """

    def __init__(
        self,
        workspace: Path | None = None,
        extra_search_paths: list[Path] | None = None,
        *,
        workspace_trusted: bool = False,
    ) -> None:
        self.workspace = workspace
        self._workspace_trusted = workspace_trusted
        self._scanner = PluginScanner.default_paths(workspace)
        if extra_search_paths:
            for p in extra_search_paths:
                self._scanner.add_path(p)

        self._plugins: dict[str, DiscoveredPlugin] = {}  # name → plugin
        self._disabled: set[str] = set()  # names disabled at runtime

        # Where this manager has wired plugins, so they can be taken back out again.
        self._hook_sinks: list[HookDispatcher] = []
        self._mcp_sinks: list[MCPServerRegistry] = []
        self._command_sinks: list[SlashCommandRegistry] = []
        self._wire_commands: Callable[[list[DiscoveredPlugin]], None] | None = None
        # MCP servers taken out of a registry whose connection is still to be closed.
        self._detached: list[tuple[MCPServerRegistry, ServerEntry]] = []
        # Serialises flushes, so a second flush() waits for cleanup that is already running
        # instead of seeing an empty list and returning while a close is still in progress.
        self._flush_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Workspace trust
    # ------------------------------------------------------------------

    @property
    def workspace_trusted(self) -> bool:
        """Whether workspace plugins may be loaded, wired and exposed."""
        return self._workspace_trusted

    def set_workspace_trusted(self, trusted: bool) -> None:
        """Record the user's workspace-trust decision (call before :meth:`load`).

        Losing trust revokes what workspace plugins already registered, not just what they
        can register next.
        """
        was_trusted = self._workspace_trusted
        self._workspace_trusted = bool(trusted)
        if was_trusted and not self._workspace_trusted:
            for plugin in list(self._plugins.values()):
                if not self._is_authorized(plugin):
                    self.revoke(plugin.name)

    def _is_authorized(self, plugin: DiscoveredPlugin) -> bool:
        """A workspace plugin needs a trusted workspace; user and extra plugins are allowed.

        An allowlist, not "anything but workspace": a plugin whose origin is not one of the
        known ones (it can only get one by being built by hand) is never authorized.
        Evaluated live rather than cached, so it also covers a plugin that reached the
        manager through some path other than :meth:`load`.
        """
        if plugin.origin == ORIGIN_WORKSPACE:
            return self._workspace_trusted
        return plugin.origin in (ORIGIN_USER, ORIGIN_EXTRA)

    def _refuse(self, plugin: DiscoveredPlugin, what: str) -> None:
        if plugin.origin == ORIGIN_WORKSPACE:
            logger.warning(
                "Refusing to %s from workspace plugin '%s': the workspace is not trusted "
                "(run 'velune trust add' to allow it).",
                what,
                plugin.name,
            )
        else:
            logger.warning(
                "Refusing to %s from plugin '%s': unrecognized origin %r.",
                what,
                plugin.name,
                plugin.origin,
            )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def load(self) -> list[DiscoveredPlugin]:
        """Scan all search paths and load plugins.  Returns newly found plugins."""
        discovered = self._scanner.scan(workspace_trusted=self._workspace_trusted)
        new: list[DiscoveredPlugin] = []
        for plugin in discovered:
            if plugin.name not in self._plugins:
                self._plugins[plugin.name] = plugin
                new.append(plugin)
        logger.info("PluginManager: %d plugin(s) loaded total.", len(self._plugins))
        return new

    def reload(self, name: str | None = None) -> list[DiscoveredPlugin]:
        """Reload a specific plugin or all plugins from disk.

        Everything the plugin(s) registered is revoked first, so a reload can never leave
        stale or duplicate registrations behind; what is found again is wired afresh into
        the registries this manager already knows. A plugin that was disabled stays
        disabled. Returns the plugins found (their MCP servers are registered but not yet
        connected, see the ``/plugin`` handler).
        """
        names = [name] if name is not None else list(self._plugins)
        for plugin_name in names:
            self.revoke(plugin_name)
            self._plugins.pop(plugin_name, None)
        found = self.load()
        for plugin in found:
            self._wire_one(plugin)
        return found

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def enable(self, name: str) -> bool:
        """Re-enable *name* and wire back exactly what it provides. False if unknown."""
        self._disabled.discard(name)
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        plugin.manifest.enabled = True
        self._wire_one(plugin)
        return True

    def disable(self, name: str) -> bool:
        """Disable *name* and revoke everything it registered. False if unknown.

        Hooks stop firing, its slash commands are removed and its MCP servers stop being
        offered or callable immediately; the server processes are closed in the background
        (``await manager.flush()`` to wait for that).
        """
        self._disabled.add(name)
        plugin = self._plugins.get(name)
        if plugin is not None:
            plugin.manifest.enabled = False
        self.revoke(name)  # idempotent, and safe for a name this manager never loaded
        return plugin is not None

    def is_active(self, name: str) -> bool:
        """True when plugin *name* is loaded, enabled and allowed to be used right now."""
        plugin = self._plugins.get(name)
        return (
            plugin is not None
            and plugin.enabled
            and name not in self._disabled
            and self._is_authorized(plugin)
        )

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled and (
            name in self._plugins and self._plugins[name].enabled
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    def revoke(self, name: str) -> dict[str, int]:
        """Remove everything plugin *name* registered, from every registry. Idempotent.

        Returns how much was removed (``hooks``, ``commands``, ``servers``). Another plugin's
        registrations are never touched: each registry keys them by owner.
        """
        hooks = sum(1 for dispatcher in self._hook_sinks if dispatcher.remove_plugin_bindings(name))
        commands = sum(len(registry.unregister_owner(name)) for registry in self._command_sinks)
        servers = 0
        for mcp_registry in self._mcp_sinks:
            for entry in mcp_registry.detach_owner(name):
                self._detached.append((mcp_registry, entry))
                servers += 1
        if servers:
            self._schedule_flush()
        if hooks or commands or servers:
            logger.info(
                "Revoked plugin '%s': %d hook set(s), %d command key(s), %d MCP server(s).",
                name,
                hooks,
                commands,
                servers,
            )
        return {"hooks": hooks, "commands": commands, "servers": servers}

    async def flush(self) -> None:
        """Close the connections of MCP servers that were revoked. Safe to call repeatedly."""
        async with self._flush_lock:
            pending, self._detached = self._detached, []
            for mcp_registry, entry in pending:
                await mcp_registry.close_entry(entry)

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop: the caller awaits flush() itself
        from velune.core.task_registry import track

        track(loop.create_task(self.flush(), name="velune.plugin_flush"))

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def attach_commands(
        self,
        registry: SlashCommandRegistry,
        wire: Callable[[list[DiscoveredPlugin]], None],
    ) -> None:
        """Tell the manager where plugin commands are registered and how to register them.

        Needed so :meth:`revoke` can remove them and :meth:`enable`/:meth:`reload` can put
        them back, without the manager knowing anything about the REPL.
        """
        self._remember(self._command_sinks, registry)
        self._wire_commands = wire

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

    def wire_hooks(self, dispatcher: HookDispatcher) -> int:
        """Give plugin hooks to *dispatcher*.  Returns the number of bindings wired.

        A workspace plugin is refused (and logged) unless the workspace is trusted. Wiring is
        idempotent: a plugin's hooks replace its previous ones, never add to them. A plugin
        whose hooks cannot be read is left with none (what it had before is removed).
        """
        self._remember(self._hook_sinks, dispatcher)
        count = 0
        for plugin in self._enabled_plugins():
            if not plugin.has_hooks:
                continue
            if not self._is_authorized(plugin):
                self._refuse(plugin, "wire hooks")
                dispatcher.remove_plugin_bindings(plugin.name)
                continue
            count += self._wire_plugin_hooks(plugin, dispatcher)
        return count

    def _wire_plugin_hooks(self, plugin: DiscoveredPlugin, dispatcher: HookDispatcher) -> int:
        try:
            hooks_file = plugin.manifest.hooks_file  # confined again where it is used
            if not hooks_file.exists():
                dispatcher.remove_plugin_bindings(plugin.name)
                return 0
            bindings = self._read_hook_bindings(hooks_file)
        except PluginPathError as exc:
            logger.warning("Refusing hooks from plugin '%s': %s", plugin.name, exc)
            dispatcher.remove_plugin_bindings(plugin.name)
            return 0
        except Exception as exc:  # the hook file is untrusted data of any shape: contain it
            logger.warning(
                "Failed to wire hooks from plugin '%s': %s: %s",
                plugin.name,
                type(exc).__name__,
                exc,
            )
            dispatcher.remove_plugin_bindings(plugin.name)
            return 0
        dispatcher.add_plugin_bindings(plugin.name, bindings)
        logger.info("Wired %d hook binding(s) from plugin '%s'.", len(bindings), plugin.name)
        return len(bindings)

    @staticmethod
    def _read_hook_bindings(hooks_file: Path) -> list[HookBinding]:
        from velune.hooks.config import _parse_bindings

        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("hooks file must contain a JSON object")
        # Support both {hooks: {...}} wrapper and bare {EventName: [...]} format
        hooks_section = data.get("hooks", data)
        if not isinstance(hooks_section, dict):
            raise ValueError("'hooks' must be a JSON object")
        bindings = _parse_bindings(hooks_section)
        for binding in bindings:
            for definition in binding.hooks:
                definition.scrub_env = True  # a plugin's hook never gets Velune's environment
        return bindings

    # ------------------------------------------------------------------
    # MCP wiring
    # ------------------------------------------------------------------

    def wire_mcp(self, mcp_registry: MCPServerRegistry) -> int:
        """Register plugin MCP servers in *mcp_registry*.  Returns the number registered.

        A workspace plugin is refused (and logged) unless the workspace is trusted. Per
        plugin it is all or nothing: every server entry is read and validated before any is
        registered, so a bad entry cannot leave its siblings half-registered. Wiring is
        idempotent; servers already registered with the same configuration are left alone
        (a connected one stays connected), changed ones are replaced.
        """
        self._remember(self._mcp_sinks, mcp_registry)
        count = 0
        for plugin in self._enabled_plugins():
            if not plugin.has_mcp:
                continue
            if not self._is_authorized(plugin):
                self._refuse(plugin, "register MCP servers")
                self._detach(mcp_registry, plugin.name)
                continue
            count += self._wire_plugin_mcp(plugin, mcp_registry)
        return count

    def _wire_plugin_mcp(self, plugin: DiscoveredPlugin, mcp_registry: MCPServerRegistry) -> int:
        try:
            mcp_file = plugin.manifest.mcp_file  # confined again where it is used
            if not mcp_file.exists():
                self._detach(mcp_registry, plugin.name)
                return 0
            configs = self._read_mcp_configs(plugin, mcp_file)
        except PluginPathError as exc:
            logger.warning("Refusing MCP servers from plugin '%s': %s", plugin.name, exc)
            self._detach(mcp_registry, plugin.name)
            return 0
        except Exception as exc:  # the .mcp.json is untrusted data of any shape: contain it
            logger.warning(
                "Failed to wire MCP from plugin '%s': %s: %s",
                plugin.name,
                type(exc).__name__,
                exc,
            )
            self._detach(mcp_registry, plugin.name)
            return 0

        current = mcp_registry.owned_entries(plugin.name)
        if {name: entry.config for name, entry in current.items()} != {
            cfg.name: cfg for cfg in configs
        }:
            self._detach(mcp_registry, plugin.name)  # changed: replace, do not accumulate
            for cfg in configs:
                mcp_registry.register(cfg, owner=plugin.name)
        return len(configs)

    @staticmethod
    def _read_mcp_configs(plugin: DiscoveredPlugin, mcp_file: Path) -> list[ServerConfig]:
        from velune.mcp.transports.base import ServerConfig

        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(".mcp.json must contain a JSON object")
        root_str = str(plugin.root)
        configs: list[ServerConfig] = []
        for server_name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            _validate_mcp_entry(server_name, entry)
            # Namespace server name: plugin-name:server-name
            cfg = ServerConfig.from_dict(f"{plugin.name}:{server_name}", entry)
            cfg.scrub_env = True  # a plugin's server never gets Velune's environment
            # Resolve ${VELUNE_PLUGIN_ROOT} in command/url/args
            for token in ("${VELUNE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}"):
                cfg.command = cfg.command.replace(token, root_str)
                cfg.url = cfg.url.replace(token, root_str)
                cfg.args = [a.replace(token, root_str) for a in cfg.args]
            configs.append(cfg)
        return configs

    def _detach(self, mcp_registry: MCPServerRegistry, owner: str) -> None:
        for entry in mcp_registry.detach_owner(owner):
            self._detached.append((mcp_registry, entry))
        if self._detached:
            self._schedule_flush()

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

    @staticmethod
    def _remember(sinks: list[Any], sink: Any) -> None:
        if not any(existing is sink for existing in sinks):
            sinks.append(sink)

    def _wire_one(self, plugin: DiscoveredPlugin) -> None:
        """Wire a single plugin into every registry this manager already knows."""
        if not (
            plugin.enabled and plugin.name not in self._disabled and self._is_authorized(plugin)
        ):
            return
        if plugin.has_hooks:
            for dispatcher in self._hook_sinks:
                self._wire_plugin_hooks(plugin, dispatcher)
        if plugin.has_mcp:
            for mcp_registry in self._mcp_sinks:
                self._wire_plugin_mcp(plugin, mcp_registry)
        if plugin.commands and self._wire_commands is not None:
            self._wire_commands([plugin])

    def _enabled_plugins(self) -> list[DiscoveredPlugin]:
        """Enabled plugins, whatever their origin (callers must check authorization)."""
        return [p for p in self._plugins.values() if p.enabled and p.name not in self._disabled]

    def _active_plugins(self) -> list[DiscoveredPlugin]:
        """Enabled plugins this session is allowed to use."""
        return [p for p in self._enabled_plugins() if self._is_authorized(p)]
