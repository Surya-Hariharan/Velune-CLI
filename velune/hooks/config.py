"""Hook configuration loader.

Loads hook bindings from (in priority order):
  1. ``<workspace>/.velune/hooks.json``   — project-level hooks
  2. ``~/.velune/hooks.json``             — user-level hooks
  3. ``<workspace>/velune.toml [hooks]``  — TOML config (simple commands)

Hook config format (mirrors Claude Code's settings.json hooks section)::

    {
      "hooks": {
        "SessionStart": [
          {
            "hooks": [
              {"type": "command", "command": "python3 my_start_hook.py", "timeout": 10}
            ]
          }
        ],
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [
              {
                "type": "command",
                "command": "python3 safety.py",
                "timeout": 5,
                "if": "Bash(rm -rf*)"
              }
            ]
          }
        ]
      }
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from velune.hooks.types import HookBinding, HookCondition, HookDefinition, HookEvent

logger = logging.getLogger("velune.hooks.config")

_EVENT_ALIASES: dict[str, HookEvent] = {
    e.value: e for e in HookEvent
}


def _parse_condition(raw_if: str) -> HookCondition | None:
    """Parse an ``if`` condition string into a HookCondition.

    Supported formats:
    - ``*``                      → match everything
    - ``Bash``                   → match Bash tool, any args
    - ``Bash(rm -rf*)``          → match Bash tool with pattern in args
    - ``Edit|Write``             → match Edit or Write tool
    - ``Edit(src/**)``           → match Edit tool with path pattern
    """
    if not raw_if or raw_if == "*":
        return HookCondition(tool="*", pattern="*")

    raw_if = raw_if.strip()
    if "(" in raw_if and raw_if.endswith(")"):
        tool_part, rest = raw_if.split("(", 1)
        pattern = rest[:-1]  # strip trailing )
        return HookCondition(tool=tool_part.strip(), pattern=pattern.strip())

    return HookCondition(tool=raw_if, pattern="*")


def _parse_hook_entry(raw: dict[str, Any]) -> HookDefinition | None:
    """Parse a single hook entry dict into a HookDefinition."""
    hook_type = raw.get("type", "command")
    command = raw.get("command", "")
    if not command:
        logger.warning("Hook entry missing 'command' field — skipped: %s", raw)
        return None

    timeout = int(raw.get("timeout", 10))
    raw_if = raw.get("if", "")
    condition = _parse_condition(raw_if) if raw_if else None
    env = {k: str(v) for k, v in raw.get("env", {}).items()}

    return HookDefinition(
        type=hook_type,
        command=command,
        timeout=timeout,
        condition=condition,
        env=env,
    )


def _parse_bindings(hooks_dict: dict[str, Any]) -> list[HookBinding]:
    """Parse the ``hooks`` dict from a config file into HookBinding objects."""
    bindings: list[HookBinding] = []

    for event_name, binding_list in hooks_dict.items():
        event = _EVENT_ALIASES.get(event_name)
        if event is None:
            logger.warning("Unknown hook event name '%s' — skipped", event_name)
            continue

        if not isinstance(binding_list, list):
            logger.warning("Hook bindings for '%s' must be a list — skipped", event_name)
            continue

        for binding_raw in binding_list:
            if not isinstance(binding_raw, dict):
                continue

            matcher = binding_raw.get("matcher", "*")
            hook_entries = binding_raw.get("hooks", [])
            if not isinstance(hook_entries, list):
                continue

            definitions: list[HookDefinition] = []
            for entry in hook_entries:
                if not isinstance(entry, dict):
                    continue
                defn = _parse_hook_entry(entry)
                if defn is not None:
                    definitions.append(defn)

            if definitions:
                bindings.append(HookBinding(event=event, hooks=definitions, matcher=matcher))

    return bindings


def load_hooks(workspace: Path | None = None) -> list[HookBinding]:
    """Load all hook bindings for the given workspace.

    Sources are loaded in reverse priority order so higher-priority sources
    override lower-priority ones (project > user).

    Args:
        workspace: Root directory of the active project. If None, only user-
                   level hooks are loaded.

    Returns:
        Flat list of HookBinding objects (may be empty).
    """
    sources: list[Path] = []

    # User-level hooks (~/.velune/hooks.json)
    user_hooks = Path.home() / ".velune" / "hooks.json"
    if user_hooks.exists():
        sources.append(user_hooks)

    # Project-level hooks (<workspace>/.velune/hooks.json)
    if workspace:
        project_hooks = Path(workspace) / ".velune" / "hooks.json"
        if project_hooks.exists():
            sources.append(project_hooks)

    all_bindings: list[HookBinding] = []
    for path in sources:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            hooks_section = data.get("hooks", {})
            if not isinstance(hooks_section, dict):
                logger.warning("'hooks' key in %s must be an object — skipped", path)
                continue
            bindings = _parse_bindings(hooks_section)
            all_bindings.extend(bindings)
            logger.debug("Loaded %d hook binding(s) from %s", len(bindings), path)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse hook config %s: %s", path, exc)
        except OSError as exc:
            logger.warning("Could not read hook config %s: %s", path, exc)

    # Also read velune.toml [hooks] section if present (simple command list)
    if workspace:
        toml_path = Path(workspace) / "velune.toml"
        toml_bindings = _load_toml_hooks(toml_path)
        all_bindings.extend(toml_bindings)

    return all_bindings


def _load_toml_hooks(toml_path: Path) -> list[HookBinding]:
    """Parse a ``[hooks]`` section from velune.toml.

    Supports a simplified flat format::

        [hooks]
        SessionStart = ["python3 start.py"]
        PreToolUse = ["python3 safety.py --timeout 5"]
    """
    if not toml_path.exists():
        return []

    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return []

    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Could not parse velune.toml for hooks: %s", exc)
        return []

    hooks_section = data.get("hooks", {})
    if not isinstance(hooks_section, dict):
        return []

    bindings: list[HookBinding] = []
    for event_name, commands in hooks_section.items():
        event = _EVENT_ALIASES.get(event_name)
        if event is None:
            continue

        if isinstance(commands, str):
            commands = [commands]
        if not isinstance(commands, list):
            continue

        definitions = [
            HookDefinition(type="command", command=cmd)
            for cmd in commands
            if isinstance(cmd, str) and cmd.strip()
        ]
        if definitions:
            bindings.append(HookBinding(event=event, hooks=definitions))

    return bindings


def get_hooks_dir(workspace: Path | None = None) -> Path:
    """Return the project-level hooks directory, creating it if needed."""
    base = Path(workspace) if workspace else Path.home() / ".velune"
    hooks_dir = base / ".velune"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir


def write_default_hooks_config(workspace: Path) -> Path:
    """Write a starter hooks.json to the workspace if none exists."""
    target = Path(workspace) / ".velune" / "hooks.json"
    if target.exists():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    starter = {
        "hooks": {
            "SessionStart": [],
            "PreToolUse": [],
            "PostToolUse": [],
            "UserPromptSubmit": [],
            "Stop": [],
            "MessageDisplay": [],
        }
    }
    target.write_text(json.dumps(starter, indent=2), encoding="utf-8")
    return target
