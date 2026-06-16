"""Velune hook system — lifecycle events for extending REPL and tool behaviour.

Quick start::

    from velune.hooks import HookDispatcher, HookEvent, HookResult

    dispatcher = HookDispatcher(workspace=Path("/my/project"))

    # Gate a tool call
    result = await dispatcher.dispatch_pre_tool_use("Bash", {"command": "..."})
    if result.blocked:
        raise PermissionError(result.block_reason)

    # Transform user input
    result = await dispatcher.dispatch_user_prompt("do the thing")
    effective_prompt = result.transformed_prompt or "do the thing"

Hook configuration lives in ``.velune/hooks.json`` (project) or
``~/.velune/hooks.json`` (user) using the same JSON schema as Claude Code's
``settings.json`` hooks section.
"""

from velune.hooks.config import load_hooks, write_default_hooks_config
from velune.hooks.dispatcher import HookDispatcher
from velune.hooks.types import (
    HookBinding,
    HookCondition,
    HookDefinition,
    HookEvent,
    HookResult,
)

__all__ = [
    "HookDispatcher",
    "HookEvent",
    "HookResult",
    "HookBinding",
    "HookCondition",
    "HookDefinition",
    "load_hooks",
    "write_default_hooks_config",
]
