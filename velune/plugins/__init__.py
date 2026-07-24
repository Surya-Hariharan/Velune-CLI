"""Velune Plugin Subsystem — declarative, markdown-based plugins.

Plugins are discovered from directories containing a manifest (see
``velune.plugins.declarative``) and are loaded and managed by
:class:`velune.plugins.manager.PluginManager`. There is no in-process or
subprocess code-execution loader: plugin surface area is markdown commands,
SKILL.md context injection, subprocess lifecycle hooks (``velune.hooks``),
and MCP server registration — no arbitrary plugin Python code runs inside or
alongside the CLI process.
"""

from velune.plugins.declarative.manifest import DeclarativePluginManifest

__all__ = [
    "DeclarativePluginManifest",
]
