"""Dynamic plugin loader discovering, parsing, and instantiating directory plugins."""

from __future__ import annotations

import json
import sys
import importlib.util
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging

from velune.plugins.schemas import PluginManifest
from velune.plugins.registry import PluginRegistry
from velune.plugins.sandbox import PluginSandbox

logger = logging.getLogger("velune.plugins.loader")


class PluginLoader:
    """Discovers, parses, and dynamically imports plugins from specified directories."""

    def __init__(self, registry: PluginRegistry, search_paths: Optional[List[Path]] = None) -> None:
        self.registry = registry
        self.search_paths = search_paths or []

    def discover_and_load(self) -> None:
        """Scan all search paths for subdirectories containing a valid manifest.json."""
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
        
        with open(manifest_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        manifest = PluginManifest(**data)
        entry_file = folder_path / manifest.entry_point

        if not entry_file.exists():
            raise FileNotFoundError(f"Entry point file {manifest.entry_point} not found in {folder_path}")

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

        # Wrap all hooks using sandbox safety wrapper
        wrapped_instance = self._wrap_instance_hooks(plugin_instance, manifest)

        # Register instance to catalog
        self.registry.register_plugin(manifest, wrapped_instance)
        logger.info("Successfully loaded and registered plugin: %s", manifest.name)

    def _wrap_instance_hooks(self, instance: Any, manifest: PluginManifest) -> Any:
        """Proxies active callbacks on instance to run inside sandbox wrappers."""
        for hook in manifest.hooks:
            method_name = f"on_{hook}" if not hook.startswith("on_") else hook
            if hasattr(instance, method_name):
                original = getattr(instance, method_name)
                # Wrap using PluginSandbox helper
                wrapped = PluginSandbox.wrap_callback(original)
                setattr(instance, method_name, wrapped)
        return instance
