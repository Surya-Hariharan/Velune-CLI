"""Plugin scanner — discovers declarative plugins in search paths.

Search order (highest priority first):
1. ``<workspace>/.velune/plugins/``    — project-specific plugins
2. ``~/.velune/plugins/``              — user global plugins
3. Extra paths supplied at runtime

Each sub-directory that contains a valid ``plugin.json`` is a plugin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from velune.plugins.declarative.command import PluginCommand, load_plugin_commands
from velune.plugins.declarative.manifest import DeclarativePluginManifest
from velune.plugins.declarative.skill import PluginSkill, load_plugin_skills

logger = logging.getLogger("velune.plugins.declarative.scanner")


@dataclass
class DiscoveredPlugin:
    """Everything Velune knows about one loaded declarative plugin."""

    manifest: DeclarativePluginManifest
    commands: list[PluginCommand] = field(default_factory=list)
    skills: list[PluginSkill] = field(default_factory=list)
    has_hooks: bool = False
    has_mcp: bool = False

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def enabled(self) -> bool:
        return self.manifest.enabled

    @property
    def root(self) -> Path:
        return self.manifest.root

    def summary(self) -> dict:
        return {
            "name": self.name,
            "version": self.manifest.version,
            "description": self.manifest.description,
            "author": self.manifest.author.name,
            "enabled": self.enabled,
            "commands": len(self.commands),
            "skills": len(self.skills),
            "hooks": self.has_hooks,
            "mcp": self.has_mcp,
            "root": str(self.root),
        }


class PluginScanner:
    """Scans search paths and returns ``DiscoveredPlugin`` objects.

    Does NOT mutate any global state — consumers decide what to do with the
    discovered plugins (register commands, wire hooks, etc.).
    """

    def __init__(self, search_paths: list[Path] | None = None) -> None:
        self._search_paths: list[Path] = list(search_paths or [])

    @classmethod
    def default_paths(cls, workspace: Path | None = None) -> PluginScanner:
        """Build a scanner with the standard search paths."""
        paths: list[Path] = []
        # Project-level (highest priority)
        if workspace:
            paths.append(workspace / ".velune" / "plugins")
        # User global
        paths.append(Path.home() / ".velune" / "plugins")
        return cls(paths)

    def add_path(self, path: Path) -> None:
        if path not in self._search_paths:
            self._search_paths.append(path)

    def scan(self) -> list[DiscoveredPlugin]:
        """Scan all search paths and return all discovered plugins.

        A plugin appearing in multiple search paths is only loaded from the
        highest-priority path (first match by name wins).
        """
        seen_names: set[str] = set()
        plugins: list[DiscoveredPlugin] = []

        for search_path in self._search_paths:
            if not search_path.exists() or not search_path.is_dir():
                continue
            logger.debug("Scanning plugin path: %s", search_path)

            for candidate in sorted(search_path.iterdir()):
                if not candidate.is_dir():
                    continue
                plugin = self._try_load(candidate)
                if plugin is None:
                    continue
                if plugin.name in seen_names:
                    logger.debug(
                        "Plugin '%s' already loaded from a higher-priority path; skipping %s.",
                        plugin.name,
                        candidate,
                    )
                    continue
                seen_names.add(plugin.name)
                plugins.append(plugin)
                logger.info(
                    "Discovered plugin '%s' v%s (%d cmd(s), %d skill(s)) at %s",
                    plugin.name,
                    plugin.manifest.version,
                    len(plugin.commands),
                    len(plugin.skills),
                    candidate,
                )

        return plugins

    def _try_load(self, directory: Path) -> DiscoveredPlugin | None:
        """Attempt to load a single plugin from *directory*."""
        manifest = DeclarativePluginManifest.load(directory)
        if manifest is None:
            return None
        if not manifest.enabled:
            logger.debug("Plugin '%s' is disabled; skipping.", manifest.name)
            return None

        commands = load_plugin_commands(manifest.commands_dir, manifest.name)
        skills = load_plugin_skills(manifest.skills_dir, manifest.name)
        has_hooks = manifest.hooks_file.exists()
        has_mcp = manifest.mcp_file.exists()

        return DiscoveredPlugin(
            manifest=manifest,
            commands=commands,
            skills=skills,
            has_hooks=has_hooks,
            has_mcp=has_mcp,
        )
