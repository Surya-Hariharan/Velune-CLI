"""Hook condition matching.

Evaluates ``if`` guards on hook entries before deciding whether to run a hook.

Supported condition syntax:
- ``*``                  → always match
- ``Bash``               → match Bash tool, any arguments
- ``Bash(rm -rf*)``      → Bash tool + fnmatch against command
- ``Edit|Write``         → match Edit or Write tool (OR logic)
- ``Edit(src/**)``       → Edit tool with fnmatch path pattern
- ``Read(~/.ssh/**)``    → Read tool with home-dir expansion in pattern
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

from velune.hooks.types import HookCondition

logger = logging.getLogger("velune.hooks.matcher")

# Map tool names to the key that holds their "primary argument" for pattern matching
_TOOL_PRIMARY_ARG: dict[str, str] = {
    "Bash": "command",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "Read": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
}


def _extract_primary_arg(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Return the primary matchable argument for a tool call."""
    key = _TOOL_PRIMARY_ARG.get(tool_name, "")
    if key:
        value = tool_input.get(key, "")
        if isinstance(value, str):
            return value
    # Fallback: concatenate all string values
    return " ".join(v for v in tool_input.values() if isinstance(v, str))


def _tool_matches(condition_tool: str, actual_tool: str) -> bool:
    """Check whether the tool guard matches the actual tool name.

    Supports pipe-separated OR lists: ``Edit|Write``.
    """
    if condition_tool == "*":
        return True
    return actual_tool in condition_tool.split("|")


def _pattern_matches(pattern: str, value: str) -> bool:
    """Check whether a value matches an fnmatch glob pattern.

    Expands ``~`` in the pattern to the real home directory so rules like
    ``Read(~/.ssh/**)`` work correctly on all platforms.
    """
    if pattern == "*":
        return True

    # Expand home directory in the pattern
    expanded = str(Path(pattern).expanduser()) if pattern.startswith("~") else pattern

    # fnmatch handles ``*`` and ``**`` (treated as ``*`` by fnmatch)
    return fnmatch.fnmatch(value, expanded) or fnmatch.fnmatch(value, pattern)


def condition_matches(
    condition: HookCondition,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """Return True if the hook condition matches this tool invocation.

    Args:
        condition:  The ``if`` guard parsed from the hook config.
        tool_name:  The actual tool being invoked (e.g. ``"Bash"``).
        tool_input: The tool's input arguments dict.

    Returns:
        True if the hook should run for this tool call.
    """
    # Tool name must match first
    if not _tool_matches(condition.tool, tool_name):
        return False

    # No pattern constraint → match any args
    if condition.pattern == "*":
        return True

    primary = _extract_primary_arg(tool_name, tool_input)
    return _pattern_matches(condition.pattern, primary)


def tool_matcher_matches(matcher: str, tool_name: str) -> bool:
    """Check whether a binding-level ``matcher`` field includes this tool.

    The matcher lives on a HookBinding (not individual entries) and acts as
    a coarse first filter before per-entry conditions are evaluated.

    Args:
        matcher:   The binding-level matcher (``"Bash"``, ``"Edit|Write"``, ``"*"``).
        tool_name: The actual tool name from the event payload.
    """
    return _tool_matches(matcher, tool_name)
