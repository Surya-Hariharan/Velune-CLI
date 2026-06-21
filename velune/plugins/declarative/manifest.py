"""Plugin manifest — loaded from ``.velune-plugin/plugin.json``."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.plugins.declarative.manifest")

# Sentinel for an unknown version
_UNKNOWN = "0.0.0"


@dataclass
class PluginAuthor:
    name: str = ""
    email: str = ""
    url: str = ""

    @classmethod
    def from_value(cls, v: Any) -> PluginAuthor:
        if isinstance(v, str):
            return cls(name=v)
        if isinstance(v, dict):
            return cls(
                name=v.get("name", ""),
                email=v.get("email", ""),
                url=v.get("url", ""),
            )
        return cls()


@dataclass
class DeclarativePluginManifest:
    """Parsed plugin.json for a declarative (non-Python) plugin."""

    name: str
    version: str = _UNKNOWN
    description: str = ""
    author: PluginAuthor = field(default_factory=PluginAuthor)
    homepage: str = ""
    repository: str = ""
    license: str = ""
    keywords: list[str] = field(default_factory=list)
    enabled: bool = True

    # Optional overrides for component paths (relative to plugin root)
    commands_path: str = "commands"
    skills_path: str = "skills"
    hooks_path: str = "hooks/hooks.json"
    mcp_path: str = ".mcp.json"

    # Raw extras in the manifest file
    extra: dict[str, Any] = field(default_factory=dict)

    # The resolved filesystem root of the plugin (set by the scanner)
    root: Path = field(default_factory=Path)

    @classmethod
    def from_dict(cls, data: dict[str, Any], root: Path) -> DeclarativePluginManifest:
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError(f"Plugin manifest at {root} is missing a 'name' field.")

        known = {
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
            "enabled",
            "commands",
            "skills",
            "hooks",
            "mcpServers",
        }
        extra = {k: v for k, v in data.items() if k not in known}

        return cls(
            name=name,
            version=str(data.get("version", _UNKNOWN)),
            description=str(data.get("description", "")),
            author=PluginAuthor.from_value(data.get("author", {})),
            homepage=str(data.get("homepage", "")),
            repository=str(data.get("repository", "")),
            license=str(data.get("license", "")),
            keywords=list(data.get("keywords", [])),
            enabled=bool(data.get("enabled", True)),
            # Custom paths from manifest (relative to plugin root)
            commands_path=str(data.get("commands", "commands")),
            skills_path=str(data.get("skills", "skills")),
            hooks_path=str(data.get("hooks", "hooks/hooks.json")),
            mcp_path=str(data.get("mcpServers", ".mcp.json")),
            extra=extra,
            root=root,
        )

    @classmethod
    def load(cls, plugin_root: Path) -> DeclarativePluginManifest | None:
        """Try to load a manifest from *plugin_root*.

        Searches for ``plugin.json`` in:
        1. ``<root>/.velune-plugin/plugin.json``   (preferred)
        2. ``<root>/plugin.json``                  (flat layout)
        """
        candidates = [
            plugin_root / ".velune-plugin" / "plugin.json",
            plugin_root / "plugin.json",
        ]
        for path in candidates:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    manifest = cls.from_dict(data, plugin_root)
                    logger.debug("Loaded plugin manifest '%s' from %s", manifest.name, path)
                    return manifest
                except Exception as exc:
                    logger.warning("Malformed plugin manifest at %s: %s", path, exc)
                    return None
        return None

    @property
    def commands_dir(self) -> Path:
        return self.root / self.commands_path

    @property
    def skills_dir(self) -> Path:
        return self.root / self.skills_path

    @property
    def hooks_file(self) -> Path:
        return self.root / self.hooks_path

    @property
    def mcp_file(self) -> Path:
        return self.root / self.mcp_path

    def __repr__(self) -> str:
        return f"<Plugin {self.name!r} v{self.version} at {self.root}>"
