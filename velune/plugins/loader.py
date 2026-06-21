"""Dynamic plugin loader discovering, parsing, and instantiating directory plugins.

Plugins are executed in an isolated subprocess via :mod:`velune.plugins.sandbox`
so they cannot access parent environment variables (API keys, credentials) or
workspace files outside their own directory.  The experimental opt-in flag is
retained but now acts as a safety prompt rather than a security gate, since the
sandbox provides real isolation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.execution.path_guard import PathGuard, PathTraversalError
from velune.plugins.registry import PluginRegistry
from velune.plugins.sandbox import PluginSandbox, PluginSandboxError

logger = logging.getLogger("velune.plugins.loader")


@dataclass
class PluginManifest:
    """Legacy manifest model for old-style plugins loaded from ``manifest.json``."""

    name: str
    version: str = "0.0.0"
    hooks: list[str] = field(default_factory=list)
    entry_point: str = "plugin.py"
    metadata: dict = field(default_factory=dict)


#: Environment variable that opts in to experimental, unsandboxed plugin loading.
EXPERIMENTAL_PLUGINS_ENV = "VELUNE_ENABLE_EXPERIMENTAL_PLUGINS"


class _SandboxProxy:
    """Proxy for a plugin instance whose hooks run in an isolated subprocess.

    Each hook call spawns a fresh subprocess via :class:`PluginSandbox`, so the
    plugin code never runs in the CLI process and cannot access credentials or
    workspace files outside its own directory.
    """

    def __init__(
        self,
        sandbox: PluginSandbox,
        folder_path: Path,
        manifest: PluginManifest,
    ) -> None:
        self._sandbox = sandbox
        self._folder_path = folder_path
        self._manifest = manifest
        self._class_name = manifest.metadata.get("class_name", "Plugin")
        for hook in manifest.hooks:
            method_name = f"on_{hook}" if not hook.startswith("on_") else hook
            setattr(self, method_name, self._make_hook(method_name))

    def _make_hook(self, hook_name: str):
        async def _hook(**kwargs: Any) -> Any:
            try:
                return self._sandbox.run_hook(
                    plugin_dir=self._folder_path,
                    entry_point=self._manifest.entry_point,
                    class_name=self._class_name,
                    hook_name=hook_name,
                    payload=kwargs,
                )
            except PluginSandboxError as exc:
                logger.error("Sandboxed plugin hook '%s' failed: %s", hook_name, exc)
                return None

        return _hook


class PluginLoader:
    """Discovers plugins from specified directories and runs them in a subprocess sandbox.

    Loading is **disabled by default** — opt in via the ``experimental`` flag or
    the ``VELUNE_ENABLE_EXPERIMENTAL_PLUGINS=1`` environment variable.  When
    enabled, plugins execute inside an isolated subprocess with no access to
    parent environment variables (credentials, API keys) and no shared memory
    with the CLI process.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        search_paths: list[Path] | None = None,
        *,
        experimental: bool = False,
        sandbox_timeout: float = 30.0,
    ) -> None:
        self.registry = registry
        self.search_paths = search_paths or []
        self.experimental = experimental
        self._sandbox = PluginSandbox(timeout=sandbox_timeout)

    def _experimental_enabled(self) -> bool:
        return self.experimental or os.environ.get(EXPERIMENTAL_PLUGINS_ENV) == "1"

    def discover_and_load(self) -> None:
        """Scan all search paths for subdirectories containing a valid manifest.json."""
        if not self._experimental_enabled():
            logger.warning(
                "Plugin discovery is DISABLED. Set %s=1 or use PluginLoader(experimental=True) "
                "to enable sandboxed plugin loading.",
                EXPERIMENTAL_PLUGINS_ENV,
            )
            return

        logger.info("Plugin loading enabled — plugins run in isolated subprocess sandbox.")

        for path in self.search_paths:
            logger.info("Scanning search path for plugins: %s", path)
            if not path.exists() or not path.is_dir():
                continue

            for item in path.iterdir():
                if item.is_dir():
                    manifest_file = item / "manifest.json"
                    if manifest_file.exists():
                        try:
                            self._load_plugin_folder(item, manifest_file)
                        except Exception as e:
                            logger.error("Failed to load plugin from folder %s: %s", item, e)

    def _load_plugin_folder(self, folder_path: Path, manifest_file: Path) -> None:
        """Parse manifest.json and register a sandboxed proxy for the plugin."""
        logger.info("Discovered plugin manifest at %s", manifest_file)

        with open(manifest_file, encoding="utf-8") as f:
            data = json.load(f)

        manifest = PluginManifest(**data)

        # Validate entry_point stays within the plugin's own folder — a crafted
        # manifest.json with entry_point="../../sensitive.py" would otherwise escape
        # even the subprocess sandbox (by loading arbitrary host files).
        try:
            entry_file = PathGuard(folder_path).validate(folder_path / manifest.entry_point)
        except PathTraversalError as exc:
            raise ValueError(
                f"Plugin '{manifest.name}' manifest.entry_point escapes plugin folder: {exc}"
            ) from exc

        if not entry_file.exists():
            raise FileNotFoundError(
                f"Entry point file {manifest.entry_point} not found in {folder_path}"
            )

        proxy = _SandboxProxy(self._sandbox, folder_path, manifest)
        self.registry.register_plugin(manifest, proxy)
        logger.info(
            "Plugin '%s' registered — hooks run in isolated subprocess (no credential access).",
            manifest.name,
        )
