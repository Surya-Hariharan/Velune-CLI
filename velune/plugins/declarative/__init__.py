"""Declarative plugin layer for Velune.

Plugins are directories with a ``plugin.json`` manifest and optional
sub-directories for commands, skills, hooks, and MCP servers.  No Python
code is loaded — everything is driven by markdown and JSON files.

Layout::

    my-plugin/
    ├── .velune-plugin/
    │   └── plugin.json        # manifest (name, version, description …)
    ├── commands/
    │   └── review.md          # /review slash command
    ├── skills/
    │   └── best-practices/
    │       └── SKILL.md       # injected into model context
    ├── hooks/
    │   └── hooks.json         # lifecycle hooks (Phase-1 format)
    └── .mcp.json              # MCP servers (Phase-2 format)
"""

from velune.plugins.declarative.command import PluginCommand, parse_command_file
from velune.plugins.declarative.manifest import DeclarativePluginManifest, PluginAuthor
from velune.plugins.declarative.scanner import DiscoveredPlugin, PluginScanner
from velune.plugins.declarative.skill import PluginSkill, parse_skill_file

__all__ = [
    "DeclarativePluginManifest",
    "PluginAuthor",
    "PluginCommand",
    "parse_command_file",
    "PluginSkill",
    "parse_skill_file",
    "PluginScanner",
    "DiscoveredPlugin",
]
