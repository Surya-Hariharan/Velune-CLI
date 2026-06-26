"""TOML-based user and workspace command loader.

Ported from Gemini CLI's FileCommandLoader / SlashCommandResolver concept.

Users can define custom slash commands in TOML files placed in:
  ~/.velune/commands/*.toml        — user-level (always loaded)
  <workspace>/.velune/commands/*.toml — workspace-level (overrides user-level)

Command TOML format
-------------------
The file name becomes the command name by default (can be overridden with the
``name`` field).  Example `.velune/commands/deploy.toml`::

    name = "deploy"
    description = "Deploy the current workspace to staging"
    usage = "/deploy [env]"
    aliases = ["dep"]

    [action]
    type = "shell"
    command = "./scripts/deploy.sh {args}"

Supported action types:
  shell   — run a shell command; ``{args}`` is replaced with the REPL args.
  message — print a fixed message to the console (useful for bookmarks/notes).
  prompt  — inject text into the conversation as a user message (coming soon).

Conflict resolution
-------------------
If the same command name is defined at both user and workspace level, the
*workspace* definition wins (closer to the project).  Built-in commands always
take absolute precedence — a user file cannot shadow ``/help`` or ``/exit``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.cli.slash_commands import SlashCommand

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 — backport exposes the same API
    import tomli as tomllib

_log = logging.getLogger("velune.cli.commands.file_commands")

_USER_COMMANDS_DIR = Path.home() / ".velune" / "commands"
_WORKSPACE_SUBDIR = Path(".velune") / "commands"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FileCommandDef:
    """Parsed representation of a single TOML command definition."""

    name: str
    description: str
    usage: str
    aliases: list[str] = field(default_factory=list)
    action_type: str = "message"  # "shell" | "message" | "prompt"
    action_command: str = ""
    action_message: str = ""
    source: str = "user"  # "user" | "workspace"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_toml_file(path: Path, source: str) -> FileCommandDef | None:
    """Parse a single TOML file into a FileCommandDef.  Returns None on error."""
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("Could not parse command file %s: %s", path, exc)
        return None

    name = str(data.get("name", path.stem)).lower().strip()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        _log.warning("Invalid command name %r in %s — skipping", name, path)
        return None

    description = str(data.get("description", f"Custom command: /{name}"))
    usage = str(data.get("usage", f"/{name} [args]"))
    aliases = [str(a).lower() for a in data.get("aliases", [])]

    action = data.get("action", {})
    if isinstance(action, dict):
        action_type = str(action.get("type", "message"))
        action_command = str(action.get("command", ""))
        action_message = str(action.get("message", ""))
    else:
        action_type = "message"
        action_command = ""
        action_message = str(action) if action else ""

    return FileCommandDef(
        name=name,
        description=description,
        usage=usage,
        aliases=aliases,
        action_type=action_type,
        action_command=action_command,
        action_message=action_message,
        source=source,
    )


# ---------------------------------------------------------------------------
# Builder — turns FileCommandDef into a live SlashCommand
# ---------------------------------------------------------------------------


def _build_slash_command(defn: FileCommandDef, console: Any) -> SlashCommand:
    """Create a SlashCommand whose handler runs the TOML-defined action."""

    async def _handler(args: str) -> None:
        if defn.action_type == "shell":
            cmd = defn.action_command.replace("{args}", args).replace("{ARGS}", args)
            if not cmd.strip():
                console.print(f"[yellow]/{defn.name}: no shell command configured.[/yellow]")
                return
            console.print(f"[dim]Running:[/dim] {cmd}")
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                output = (stdout or b"").decode(errors="replace").strip()
                if output:
                    console.print(output)
                rc = proc.returncode or 0
                if rc != 0:
                    console.print(f"[red]Exit code {rc}[/red]")
            except TimeoutError:
                console.print(f"[red]/{defn.name}: command timed out (120s)[/red]")
            except Exception as exc:
                console.print(f"[red]/{defn.name}: {exc}[/red]")

        elif defn.action_type == "message":
            msg = defn.action_message or f"[dim]/{defn.name} — no message configured.[/dim]"
            console.print(msg)

        else:
            console.print(
                f"[yellow]/{defn.name}: unsupported action type {defn.action_type!r}.[/yellow]"
            )

    return SlashCommand(
        name=defn.name,
        aliases=defn.aliases,
        description=f"{defn.description} [dim]({defn.source})[/dim]",
        usage=defn.usage,
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class FileCommandLoader:
    """Discovers and loads TOML command files from user and workspace directories."""

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    def load(self, console: Any, existing_names: set[str]) -> list[SlashCommand]:
        """Return SlashCommands loaded from TOML files.

        Workspace commands override same-named user commands.
        Built-in commands (in *existing_names*) are never shadowed.
        """
        # User-level commands
        user_defs: dict[str, FileCommandDef] = {}
        for path in sorted(_USER_COMMANDS_DIR.glob("*.toml")):
            defn = _parse_toml_file(path, source="user")
            if defn:
                user_defs[defn.name] = defn

        # Workspace-level commands (override user)
        workspace_defs: dict[str, FileCommandDef] = {}
        if self._workspace:
            ws_dir = self._workspace / _WORKSPACE_SUBDIR
            for path in sorted(ws_dir.glob("*.toml")):
                defn = _parse_toml_file(path, source="workspace")
                if defn:
                    workspace_defs[defn.name] = defn

        # Merge: workspace overrides user
        merged: dict[str, FileCommandDef] = {**user_defs, **workspace_defs}

        commands: list[SlashCommand] = []
        for name, defn in merged.items():
            if name in existing_names:
                _log.debug("Skipping file command %r — shadowed by built-in command", name)
                continue
            commands.append(_build_slash_command(defn, console))
            if defn.source == "workspace":
                _log.debug("Loaded workspace command: /%s", name)
            else:
                _log.debug("Loaded user command: /%s", name)

        return commands
