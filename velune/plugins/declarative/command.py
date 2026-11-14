"""Plugin command parser — reads ``.md`` files from a plugin's ``commands/`` dir.

Each file becomes a slash command.  The filename (minus ``.md``) is the command
name.  Optional YAML frontmatter controls metadata:

    ---
    description: Review code quality
    args: [file-path]            # positional arg hints shown in /help
    aliases: [rv]                # optional short aliases
    ---

    Review the file @$1 for security issues, performance, and style.

``$1``, ``$2`` … are substituted with the positional arguments the user passes
to the slash command.  ``$*`` expands to all arguments joined by spaces.
``${VELUNE_PLUGIN_ROOT}`` expands to the plugin's filesystem root.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.plugins.declarative.paths import is_within

logger = logging.getLogger("velune.plugins.declarative.command")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class PluginCommand:
    """A slash command contributed by a plugin."""

    name: str
    plugin_name: str
    description: str = ""
    usage: str = ""
    aliases: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    body: str = ""  # markdown body after frontmatter
    source_file: Path = field(default_factory=Path)

    @property
    def full_name(self) -> str:
        """Canonical name: same as ``name`` (no namespace — keeps it simple)."""
        return self.name

    @property
    def help_label(self) -> str:
        """Label shown in /help: "(plugin:name)"."""
        return f"(plugin:{self.plugin_name})"

    def render(self, args: str, plugin_root: Path) -> str:
        """Substitute positional args and env-vars into the command body."""
        positional = args.split() if args else []
        text = self.body

        # ${VELUNE_PLUGIN_ROOT}  and  ${CLAUDE_PLUGIN_ROOT} (CC compat)
        text = text.replace("${VELUNE_PLUGIN_ROOT}", str(plugin_root))
        text = text.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))

        # $* — all args
        text = text.replace("$*", args)

        # $1, $2, … $9
        for i, val in enumerate(positional, 1):
            text = text.replace(f"${i}", val)

        # Remove leftover placeholders ($1 through $9 that had no argument)
        text = re.sub(r"\$[1-9]", "", text)

        return text.strip()


def _string_list(value: Any) -> list[str]:
    """A frontmatter value as a list of strings. Anything that is not text is dropped.

    Frontmatter is whatever the plugin's author wrote (``aliases: [1, null]``,
    ``aliases: 5``), so it is never trusted to be a list of strings.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def parse_command_file(path: Path, plugin_name: str) -> PluginCommand | None:
    """Parse a command ``.md`` file and return a ``PluginCommand``.

    Returns ``None`` on parse failure (logs a warning instead of raising).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Cannot read command file %s: %s", path, exc)
        return None

    fm: dict[str, Any] = {}
    body = raw

    m = _FRONTMATTER_RE.match(raw)
    if m:
        try:
            fm = _parse_simple_yaml(m.group(1))
        except ValueError as exc:
            # Malformed frontmatter is the author's data: skip this command, keep the rest.
            logger.warning(
                "Plugin '%s': command file %s has invalid frontmatter (%s); skipped.",
                plugin_name,
                path.name,
                exc,
            )
            return None
        body = raw[m.end() :]

    # Command name from filename (strip .md)
    name = path.stem.lower().replace(" ", "-")

    description = str(fm.get("description", fm.get("desc", "")))
    aliases = _string_list(fm.get("aliases", fm.get("alias", [])))
    args = _string_list(fm.get("args", fm.get("argument-hint", [])))

    # Build usage string
    usage = f"/{name}"
    if args:
        usage += " " + " ".join(f"<{a}>" for a in args)

    return PluginCommand(
        name=name,
        plugin_name=plugin_name,
        description=description,
        usage=usage,
        aliases=aliases,
        args=args,
        body=body.strip(),
        source_file=path,
    )


def load_plugin_commands(
    commands_dir: Path, plugin_name: str, plugin_root: Path | None = None
) -> list[PluginCommand]:
    """Scan *commands_dir* and return all valid ``PluginCommand`` objects.

    With *plugin_root*, a file that resolves outside the plugin (a symlink) is skipped: a
    command's text is sent to the model, so a link to a secret file must never be read.
    """
    if not commands_dir.exists() or not commands_dir.is_dir():
        return []

    commands: list[PluginCommand] = []
    for md_file in sorted(commands_dir.glob("*.md")):
        if plugin_root is not None and not is_within(md_file, plugin_root):
            logger.warning(
                "Plugin '%s': command file %s resolves outside the plugin; skipped.",
                plugin_name,
                md_file.name,
            )
            continue
        cmd = parse_command_file(md_file, plugin_name)
        if cmd is not None:
            commands.append(cmd)
            logger.debug("Loaded plugin command '/%s' from %s", cmd.name, md_file)

    return commands


# ---------------------------------------------------------------------------
# Minimal YAML-subset parser (no external dependency)
# ---------------------------------------------------------------------------


def _load_yaml_mapping(text: str) -> dict[str, Any] | None:
    """Parse *text* with PyYAML as a mapping; ``None`` when PyYAML is not installed.

    Raises ``ValueError`` for anything that is not a usable mapping: invalid YAML, an unsafe
    tag, a list or a bare scalar. Frontmatter is whatever a plugin's author wrote, so none of
    those may escape as another exception type, and only the *kind* of failure is reported.
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        return None
    try:
        loaded = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"invalid YAML ({type(exc).__name__})") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"frontmatter must be a mapping, not {type(loaded).__name__}")
    return loaded


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the tiny YAML subset used in plugin frontmatter.

    Supports:
    - String scalars: ``key: value``
    - Quoted strings: ``key: "value"``
    - Inline lists: ``key: [a, b, c]``
    - Block lists:
        key:
          - item1
          - item2
    - Booleans: ``true`` / ``false``
    """
    loaded = _load_yaml_mapping(text)
    if loaded is not None:
        return loaded

    # Pure-stdlib fallback for the common subset
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Block list item
        if stripped.startswith("- ") and current_key and current_list is not None:
            current_list.append(stripped[2:].strip().strip("\"'"))
            result[current_key] = current_list
            continue

        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()

            # Inline list
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1]
                items = [i.strip().strip("\"'") for i in inner.split(",") if i.strip()]
                result[key] = items
                current_key = None
                current_list = None
                continue

            # Start of block list
            if val == "":
                current_key = key
                current_list = []
                result[key] = current_list
                continue

            current_key = None
            current_list = None

            # Strip quotes
            val = val.strip("\"'")

            # Booleans
            if val.lower() == "true":
                result[key] = True
            elif val.lower() == "false":
                result[key] = False
            else:
                result[key] = val

    return result
