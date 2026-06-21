"""Plugin catalog registry tracking active plugin components and hook associations."""

from __future__ import annotations

import json
import logging
from typing import Any

from velune.plugins.declarative.manifest import DeclarativePluginManifest
from velune.plugins.hooks import PluginHookDispatcher

logger = logging.getLogger("velune.plugins.registry")


def _extract_hook_names(manifest: Any) -> list[str]:
    """Return hook names from *manifest*, handling both manifest shapes.

    - ``DeclarativePluginManifest``: reads hook names from ``hooks_file`` JSON.
    - Legacy manifest (old-style ``manifest.json``): reads ``manifest.hooks`` list.
    """
    # Declarative manifest: hooks live in an external JSON file
    if isinstance(manifest, DeclarativePluginManifest):
        hf = manifest.hooks_file
        if not hf.exists():
            return []
        try:
            data = json.loads(hf.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return list(data.keys())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read hooks file %s: %s", hf, exc)
        return []

    # Legacy manifest: hooks is an inline list[str]
    hooks = getattr(manifest, "hooks", None)
    if isinstance(hooks, list):
        return [str(h) for h in hooks]
    return []


class PluginRegistry:
    """Stores all loaded manifests and associated hook boundaries."""

    def __init__(self, hook_dispatcher: PluginHookDispatcher | None = None) -> None:
        self.hook_dispatcher = hook_dispatcher or PluginHookDispatcher()
        self._manifests: dict[str, Any] = {}
        self._instances: dict[str, Any] = {}

    def register_plugin(self, manifest: Any, instance: Any) -> None:
        """Saves a loaded plugin manifest and instantiates hook callbacks."""
        self._manifests[manifest.name] = manifest
        self._instances[manifest.name] = instance

        for hook_name in _extract_hook_names(manifest):
            method_name = f"on_{hook_name}" if not hook_name.startswith("on_") else hook_name
            # Strip 'on_' prefix for dispatcher hook names
            clean_hook_name = hook_name
            if clean_hook_name.startswith("on_"):
                clean_hook_name = clean_hook_name[3:]

            if hasattr(instance, method_name):
                callback = getattr(instance, method_name)
                if callable(callback):
                    self.hook_dispatcher.register_hook(clean_hook_name, callback)
                    logger.info(
                        "Associated plugin %s with hook: %s", manifest.name, clean_hook_name
                    )
            else:
                logger.warning(
                    "Plugin %s declared hook %s but lacks method %s",
                    manifest.name,
                    hook_name,
                    method_name,
                )

    def get_plugin(self, name: str) -> Any | None:
        """Fetch active plugin instance."""
        return self._instances.get(name)

    def list_plugins(self) -> list[Any]:
        """Returns metadata list of loaded active plugins."""
        return list(self._manifests.values())
