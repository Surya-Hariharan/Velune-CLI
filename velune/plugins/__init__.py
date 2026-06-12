"""Velune Plugin Subsystem - dynamic plugins, hooks, and registry.

WARNING: This subsystem does NOT provide sandbox isolation. Plugin loading is
experimental, disabled by default, and runs plugins in-process with full
process privileges. See velune/plugins/loader.py and
VELUNE_ARCHITECTURE_BIBLE.md §9.6.
"""

from velune.plugins.hooks import PluginHookDispatcher
from velune.plugins.loader import EXPERIMENTAL_PLUGINS_ENV, PluginLoader
from velune.plugins.registry import PluginRegistry
from velune.plugins.schemas import PluginManifest

__all__ = [
    "PluginManifest",
    "PluginHookDispatcher",
    "PluginRegistry",
    "PluginLoader",
    "EXPERIMENTAL_PLUGINS_ENV",
]
