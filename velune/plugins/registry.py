"""Plugin catalog registry tracking active plugin components and hook associations."""

from __future__ import annotations

from typing import Dict, List, Any
import logging

from velune.plugins.schemas import PluginManifest
from velune.plugins.hooks import PluginHookDispatcher

logger = logging.getLogger("velune.plugins.registry")


class PluginRegistry:
    """Stores all loaded manifests and associated hook boundaries."""

    def __init__(self, hook_dispatcher: Optional[PluginHookDispatcher] = None) -> None:
        self.hook_dispatcher = hook_dispatcher or PluginHookDispatcher()
        self._manifests: Dict[str, PluginManifest] = {}
        self._instances: Dict[str, Any] = {}

    def register_plugin(self, manifest: PluginManifest, instance: Any) -> None:
        """Saves a loaded plugin manifest and instantiates hook callbacks."""
        self._manifests[manifest.name] = manifest
        self._instances[manifest.name] = instance

        # Search for callable hook methods based on manifest hooks list
        for hook_name in manifest.hooks:
            method_name = f"on_{hook_name}" if not hook_name.startswith("on_") else hook_name
            # Strip 'on_' prefix for dispatcher hook names
            clean_hook_name = hook_name
            if clean_hook_name.startswith("on_"):
                clean_hook_name = clean_hook_name[3:]
                
            if hasattr(instance, method_name):
                callback = getattr(instance, method_name)
                if callable(callback):
                    self.hook_dispatcher.register_hook(clean_hook_name, callback)
                    logger.info("Associated plugin %s with hook: %s", manifest.name, clean_hook_name)
            else:
                logger.warning(
                    "Plugin %s declared hook %s but lacks method %s",
                    manifest.name,
                    hook_name,
                    method_name,
                )

    def get_plugin(self, name: str) -> Optional[Any]:
        """Fetch active plugin instance."""
        return self._instances.get(name)

    def list_plugins(self) -> List[PluginManifest]:
        """Returns metadata list of loaded active plugins."""
        return list(self._manifests.values())
