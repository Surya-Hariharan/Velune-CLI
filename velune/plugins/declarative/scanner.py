"""Plugin scanner — discovers declarative plugins in search paths.

Search order:
1. ``<workspace>/.velune/plugins/``    — project-specific plugins
2. ``~/.velune/plugins/``              — user global plugins
3. Extra paths supplied at runtime

If two plugins share a name, the user's wins over a repository's (see ``scan``).

Each sub-directory that contains a valid ``plugin.json`` is a plugin.

Trust: a workspace plugin is project-controlled content (its hooks and MCP servers run local
commands), so the workspace search path is only scanned when the caller says the workspace
is trusted. Every discovered plugin records the *origin* of the path it came from, so later
stages can refuse a workspace plugin without guessing from names or order.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from velune.plugins.declarative.command import PluginCommand, load_plugin_commands
from velune.plugins.declarative.manifest import DeclarativePluginManifest
from velune.plugins.declarative.skill import PluginSkill, load_plugin_skills

logger = logging.getLogger("velune.plugins.declarative.scanner")

#: ``<workspace>/.velune/plugins`` -- controlled by whoever wrote the repository.
ORIGIN_WORKSPACE = "workspace"
#: ``~/.velune/plugins`` -- owned by the user.
ORIGIN_USER = "user"
#: A path the embedding code added explicitly (``PluginManager(extra_search_paths=...)``).
ORIGIN_EXTRA = "extra"

#: The only origins there are. Anything else is rejected where a path is registered, and
#: ``PluginManager`` never authorizes a plugin whose origin is not one of these.
ORIGINS = frozenset({ORIGIN_USER, ORIGIN_WORKSPACE, ORIGIN_EXTRA})

#: When one directory is registered under several origins (``workspace == HOME`` makes
#: ``<workspace>/.velune/plugins`` and ``~/.velune/plugins`` the same folder) the highest
#: rank wins, whatever the order of registration:
#:
#: * ``user`` wins over everything: it is the user's own folder, so it must stay available
#:   whatever the workspace trust;
#: * ``workspace`` wins over ``extra``: a repository's folder never becomes trusted just
#:   because embedding code also listed it as an extra path.
_ORIGIN_PRECEDENCE = {ORIGIN_USER: 2, ORIGIN_WORKSPACE: 1, ORIGIN_EXTRA: 0}

#: When two plugins share a *name*, the one from the higher-ranked origin is used and the
#: other is skipped with a warning, whatever order the folders were searched in. A repository
#: must never replace a plugin the user (or the embedding code) installed, so ``workspace`` is
#: the lowest. (This is separate from ``_ORIGIN_PRECEDENCE``, which settles what origin a
#: *directory* has when it is registered more than once.)
_NAME_PRECEDENCE = {ORIGIN_USER: 2, ORIGIN_EXTRA: 1, ORIGIN_WORKSPACE: 0}

_WORKSPACE_PLUGINS_SUBDIR = (".velune", "plugins")


def _path_key(path: Path) -> str:
    """A key that is equal for two spellings of the same physical directory.

    ``resolve()`` folds ``..``, relative spellings and symlinks; ``normcase`` folds case on
    case-insensitive platforms (Windows).
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):  # unreadable path or a symlink loop: compare as spelled
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


def workspace_plugins_dir(workspace: Path) -> Path:
    """Where a workspace keeps its own plugins."""
    return workspace.joinpath(*_WORKSPACE_PLUGINS_SUBDIR)


def has_workspace_plugins(workspace: Path) -> bool:
    """True when *workspace* ships at least one plugin manifest.

    Used to decide whether the workspace-trust prompt is needed: a repository whose only
    project config is a plugin still carries hooks and MCP servers that run local commands.
    """
    try:
        plugins_dir = workspace_plugins_dir(workspace)
        if not plugins_dir.is_dir():
            return False
        return any(
            manifest.exists()
            for candidate in plugins_dir.iterdir()
            if candidate.is_dir()
            for manifest in DeclarativePluginManifest.manifest_candidates(candidate)
        )
    except OSError:
        return False


@dataclass
class DiscoveredPlugin:
    """Everything Velune knows about one loaded declarative plugin."""

    manifest: DeclarativePluginManifest
    commands: list[PluginCommand] = field(default_factory=list)
    skills: list[PluginSkill] = field(default_factory=list)
    has_hooks: bool = False
    has_mcp: bool = False
    #: Where the plugin was found. Defaults to the *workspace* origin so a plugin built
    #: without saying where it came from is treated as project-controlled, not trusted.
    origin: str = ORIGIN_WORKSPACE

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
            "origin": self.origin,
        }


class PluginScanner:
    """Scans search paths and returns ``DiscoveredPlugin`` objects.

    Does NOT mutate any global state — consumers decide what to do with the
    discovered plugins (register commands, wire hooks, etc.).
    """

    def __init__(self, search_paths: list[Path] | None = None) -> None:
        self._search_paths: list[Path] = []
        # The physical identity of each search path, taken when it was added (parallel to
        # ``_search_paths``), and the origin of each identity. See ``_path_key``.
        self._keys: list[str] = []
        self._origins: dict[str, str] = {}
        # Paths given to the constructor were chosen by the caller, like ``add_path``.
        for path in search_paths or []:
            self.add_path(path)

    @classmethod
    def default_paths(cls, workspace: Path | None = None) -> PluginScanner:
        """Build a scanner with the standard search paths."""
        scanner = cls()
        # Project-level (highest priority)
        if workspace:
            scanner.add_path(workspace_plugins_dir(workspace), origin=ORIGIN_WORKSPACE)
        # User global
        scanner.add_path(Path.home() / ".velune" / "plugins", origin=ORIGIN_USER)
        return scanner

    def add_path(self, path: Path, origin: str = ORIGIN_EXTRA) -> None:
        """Add a search path. A directory already present keeps its position in the search
        order; its origin becomes the higher-ranked of the two (``user`` > ``workspace`` >
        ``extra``), so the result does not depend on the order paths were added in.

        *origin* must be one of :data:`ORIGINS`. An unrecognized value raises ``ValueError``
        rather than being given a default, so no path can end up with an origin nobody
        decided on.
        """
        if origin not in ORIGINS:
            raise ValueError(
                f"Unknown plugin origin {origin!r}; expected one of {sorted(ORIGINS)}."
            )
        key = _path_key(path)
        known = self._origins.get(key)
        if known is None:
            self._search_paths.append(path)
            self._keys.append(key)
            self._origins[key] = origin
        elif _ORIGIN_PRECEDENCE[origin] > _ORIGIN_PRECEDENCE[known]:
            self._origins[key] = origin

    def scan(self, *, workspace_trusted: bool = False) -> list[DiscoveredPlugin]:
        """Scan the search paths and return the plugins found.

        Two plugins with the same name are never both used: the higher-ranked origin wins
        (user, then extra, then workspace), and within one origin the first in search order.
        The skipped one is reported with a warning. The result does not depend on the order
        the search paths were added in.

        Workspace paths are skipped unless *workspace_trusted* is true (fail closed: the
        default is untrusted), so an untrusted repository's plugins are never even parsed,
        and cannot occupy a name that a user-level plugin should hold.
        """
        found: list[DiscoveredPlugin] = []

        for search_path, key in zip(self._search_paths, self._keys, strict=True):
            if _path_key(search_path) != key:
                # The origin was decided for the directory this path named when it was
                # added. If it now names another one (a link was swapped), that decision
                # does not apply, and guessing an origin would fail open: skip it.
                logger.warning(
                    "Plugin path %s no longer refers to the directory it was registered "
                    "for; it was not scanned.",
                    search_path,
                )
                continue
            origin = self._origins[key]
            if origin == ORIGIN_WORKSPACE and not workspace_trusted:
                # The origin is explicit, so nothing is inferred from how deep the path is
                # or where its workspace would be; this only words an informational line.
                if search_path.is_dir():
                    logger.info(
                        "Workspace plugins in %s were not loaded: the workspace is not "
                        "trusted (run 'velune trust add' to allow them).",
                        search_path,
                    )
                continue
            if not search_path.exists() or not search_path.is_dir():
                continue
            logger.debug("Scanning plugin path: %s", search_path)

            for candidate in sorted(search_path.iterdir()):
                if not candidate.is_dir():
                    continue
                plugin = self._try_load(candidate, origin)
                if plugin is not None:
                    found.append(plugin)

        return self._resolve_names(found)

    @staticmethod
    def _resolve_names(found: list[DiscoveredPlugin]) -> list[DiscoveredPlugin]:
        """Keep one plugin per name (see :meth:`scan`), in discovery order."""
        winners: dict[str, DiscoveredPlugin] = {}
        for plugin in found:
            held = winners.get(plugin.name)
            if held is None:
                winners[plugin.name] = plugin
                continue
            if _NAME_PRECEDENCE.get(plugin.origin, -1) > _NAME_PRECEDENCE.get(held.origin, -1):
                winner, loser = plugin, held
                winners[plugin.name] = plugin
            else:
                winner, loser = held, plugin
            logger.warning(
                "Plugin '%s' at %s (%s) was skipped: a plugin of that name from %s is used "
                "instead (%s).",
                loser.name,
                loser.root,
                loser.origin,
                winner.origin,
                winner.root,
            )
        ordered = [plugin for plugin in found if winners[plugin.name] is plugin]
        for plugin in ordered:
            logger.info(
                "Discovered plugin '%s' v%s (%d cmd(s), %d skill(s)) at %s",
                plugin.name,
                plugin.manifest.version,
                len(plugin.commands),
                len(plugin.skills),
                plugin.root,
            )
        return ordered

    def _try_load(self, directory: Path, origin: str = ORIGIN_WORKSPACE) -> DiscoveredPlugin | None:
        """Attempt to load a single plugin from *directory*."""
        manifest = DeclarativePluginManifest.load(directory)
        if manifest is None:
            return None
        if not manifest.enabled:
            logger.debug("Plugin '%s' is disabled; skipping.", manifest.name)
            return None

        # One broken plugin must not cost the others. Everything read here is data written by
        # the plugin's author, of any shape, so *any* failure while reading this plugin skips
        # it (it then registers nothing) and says why; the type of the failure is reported,
        # never the data.
        try:
            commands = load_plugin_commands(
                manifest.commands_dir, manifest.name, plugin_root=manifest.root
            )
            skills = load_plugin_skills(
                manifest.skills_dir, manifest.name, plugin_root=manifest.root
            )
            has_hooks = manifest.hooks_file.exists()
            has_mcp = manifest.mcp_file.exists()
        except Exception as exc:
            logger.warning(
                "Plugin '%s' at %s was skipped: %s: %s",
                manifest.name,
                directory,
                type(exc).__name__,
                exc,
            )
            return None

        return DiscoveredPlugin(
            manifest=manifest,
            commands=commands,
            skills=skills,
            has_hooks=has_hooks,
            has_mcp=has_mcp,
            origin=origin,
        )
