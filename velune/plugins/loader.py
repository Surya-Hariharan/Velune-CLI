"""Dynamic plugin loader discovering, parsing, and instantiating directory plugins.

SECURITY: Plugin sandboxing is **designed but not yet implemented** (see
VELUNE_ARCHITECTURE_BIBLE.md §9.6). Plugins are loaded in-process via importlib
and run with FULL process privileges — unrestricted filesystem, network, and
credential access — exactly as if their source were executed directly. Because
no isolation exists yet, discovery is gated behind an explicit experimental
opt-in (``VELUNE_ENABLE_EXPERIMENTAL_PLUGINS=1`` or ``experimental=True``) and
is unreachable from any shipped CLI command.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from velune.execution.path_guard import PathGuard, PathTraversalError
from velune.plugins.registry import PluginRegistry
from velune.plugins.schemas import PluginManifest

logger = logging.getLogger("velune.plugins.loader")

#: Environment variable that opts in to experimental, unsandboxed plugin loading.
EXPERIMENTAL_PLUGINS_ENV = "VELUNE_ENABLE_EXPERIMENTAL_PLUGINS"


class PluginLoader:
    """Discovers, parses, and dynamically imports plugins from specified directories.

    Loading is **disabled by default**. Because plugins run with full process
    privileges (no sandbox is implemented yet), :meth:`discover_and_load` is a
    no-op unless the caller explicitly opts in via the ``experimental`` flag or
    the ``VELUNE_ENABLE_EXPERIMENTAL_PLUGINS=1`` environment variable.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        search_paths: list[Path] | None = None,
        *,
        experimental: bool = False,
    ) -> None:
        self.registry = registry
        self.search_paths = search_paths or []
        self.experimental = experimental

    def _experimental_enabled(self) -> bool:
        """Return True only when unsandboxed plugin loading has been opted into."""
        return self.experimental or os.environ.get(EXPERIMENTAL_PLUGINS_ENV) == "1"

    def discover_and_load(self) -> None:
        """Scan all search paths for subdirectories containing a valid manifest.json.

        Hard guard: does nothing unless experimental plugin loading is enabled,
        because loaded plugins are NOT sandboxed and run with full process
        privileges.
        """
        if not self._experimental_enabled():
            logger.warning(
                "Plugin discovery is DISABLED. Velune plugin sandboxing is not yet "
                "implemented: loaded plugins run IN-PROCESS with FULL process "
                "privileges (filesystem, network, credentials). To enable plugin "
                "loading anyway, set %s=1 or construct PluginLoader(experimental=True). "
                "Only load plugins you trust as you would arbitrary Python code.",
                EXPERIMENTAL_PLUGINS_ENV,
            )
            return

        logger.warning(
            "⚠ EXPERIMENTAL plugin loading is ENABLED. Plugins run with FULL "
            "process privileges; sandboxing is NOT yet implemented. Only load "
            "plugins you trust as you would arbitrary Python code."
        )

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
        """Parses manifest.json and loads the python entry point module dynamically."""
        logger.info("Discovered plugin manifest at %s", manifest_file)

        with open(manifest_file, encoding="utf-8") as f:
            data = json.load(f)

        manifest = PluginManifest(**data)

        # Validate entry_point stays within the plugin's own folder — a crafted
        # manifest.json with entry_point="../../sensitive.py" would otherwise escape.
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

        # Dynamic import setup
        module_name = f"velune.plugins.dynamic.{manifest.name}"
        spec = importlib.util.spec_from_file_location(module_name, str(entry_file))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not build import spec for entry file: {entry_file}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        # Execute the module
        spec.loader.exec_module(module)

        # Look for Plugin class (typically named Plugin, or based on manifest metadata)
        plugin_class_name = manifest.metadata.get("class_name", "Plugin")
        if not hasattr(module, plugin_class_name):
            raise AttributeError(f"Plugin class '{plugin_class_name}' not found in entry module.")

        plugin_class = getattr(module, plugin_class_name)
        plugin_instance = plugin_class()

        # Wrap all hooks in a try/except boundary. NOTE: this only prevents a
        # crashing plugin from taking down the process — it provides NO security
        # isolation. The plugin still runs with full process privileges.
        wrapped_instance = self._wrap_instance_hooks(plugin_instance, manifest)

        # Register instance to catalog
        self.registry.register_plugin(manifest, wrapped_instance)
        logger.warning(
            "⚠ Plugin '%s' loaded with FULL PROCESS PRIVILEGES — sandboxing is "
            "not yet implemented. It can read/write any file, open any network "
            "connection, and access credentials. Only load plugins you trust as "
            "you would arbitrary Python code.",
            manifest.name,
        )

    def _wrap_instance_hooks(self, instance: Any, manifest: PluginManifest) -> Any:
        """Proxies active callbacks on instance to run inside a try-except safety boundary."""
        for hook in manifest.hooks:
            method_name = f"on_{hook}" if not hook.startswith("on_") else hook
            if hasattr(instance, method_name):
                original = getattr(instance, method_name)

                # Inline async wrapper — catches all plugin failures without crashing the process
                async def _safe_callback(
                    *args: Any, _orig: Any = original, _name: str = method_name, **kwargs: Any
                ) -> Any:
                    try:
                        return await _orig(*args, **kwargs)
                    except Exception as e:
                        logger.error("Plugin callback %s failed: %s", _name, e)
                        return None

                setattr(instance, method_name, _safe_callback)
        return instance
