"""What one agent may do: a permission profile narrowed by the user's settings.

A profile's tool groups map onto the permissions tools already declare
(``BaseTool.get_required_permissions``), so no list of tool names is kept here.
The policy only ever *narrows*: it decides which tools an agent is offered and
refuses calls outside them, while the session's permission gate still decides
every write and command exactly as it does in chat.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from velune.orchestration.tool_loop import Approver, Denied
from velune.permissions import ExecutionMode
from velune.swarm.library import PermissionProfile
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission

P = ToolPermission
_GROUPS: dict[str, frozenset[ToolPermission]] = {
    "read": frozenset({P.FILESYSTEM_READ, P.GIT_READ}),
    "search": frozenset({P.FILESYSTEM_READ, P.GIT_READ}),
    "write": frozenset({P.FILESYSTEM_WRITE, P.GIT_WRITE}),
    "shell": frozenset({P.TERMINAL_EXECUTE}),
    "web": frozenset({P.NETWORK_ACCESS}),
    "integrations": frozenset({P.INTEGRATION_READ, P.INTEGRATION_WRITE}),
}
_CHANGES_STATE = _GROUPS["write"] | _GROUPS["shell"] | {P.INTEGRATION_WRITE}

#: Swarm scratch tools (velune/swarm/store.py) touch only the run's own folder.
SCRATCH_TOOLS = frozenset({"swarm_note_write", "swarm_note_read", "swarm_result_read"})


@dataclass(frozen=True)
class SwarmPolicy:
    profile: str
    allowed: frozenset[ToolPermission]
    tool_names: frozenset[str]  # exact tool names the profile lists
    mcp: tuple[str, ...]
    write: str
    commands: str
    max_tool_calls: int

    @classmethod
    def build(cls, profile: PermissionProfile, *, mode: ExecutionMode | None = None) -> SwarmPolicy:
        """The profile, narrowed by the execution mode (never widened)."""
        allowed: set[ToolPermission] = set()
        names: set[str] = set()
        for entry in profile.tools:
            if entry in _GROUPS:
                allowed |= _GROUPS[entry]
            else:
                names.add(entry)
        write, commands = profile.write, profile.commands
        if write != "workspace":
            allowed -= _GROUPS["write"]
        if commands == "none":
            allowed -= _GROUPS["shell"]
        if profile.mcp:
            allowed.add(P.NETWORK_ACCESS)  # how MCP tools declare themselves
        if mode is ExecutionMode.PLAN:
            # Planning changes no project files and runs no commands.
            allowed -= _CHANGES_STATE
            write = "proposal" if write == "workspace" else write
            commands = "none"
        return cls(
            profile=profile.slug,
            allowed=frozenset(allowed),
            tool_names=frozenset(names),
            mcp=profile.mcp,
            write=write,
            commands=commands,
            max_tool_calls=profile.max_tool_calls,
        )

    def for_agent(self, *, can_write: bool, can_act: bool = False) -> SwarmPolicy:
        """The policy for one agent.

        File changes and commands follow *can_write*; changes made through an
        integration follow *can_act*.
        """
        removed: frozenset[ToolPermission] = frozenset()
        if not can_write:
            removed |= _GROUPS["write"] | _GROUPS["shell"]
        if not can_act:
            removed |= {P.INTEGRATION_WRITE}
        narrowed = replace(self, allowed=self.allowed - removed)
        if can_write:
            return narrowed
        return replace(
            narrowed, write="none" if self.write == "workspace" else self.write, commands="none"
        )

    @property
    def can_write(self) -> bool:
        return bool(self.allowed & _GROUPS["write"])

    def permits(self, name: str, permissions: set[ToolPermission] | frozenset) -> bool:
        if name in SCRATCH_TOOLS:
            return True
        if name in self.tool_names:
            return True
        return bool(permissions) and set(permissions) <= self.allowed

    def tool_registry(self, base: ToolRegistry | None) -> ToolRegistry:
        """A registry offering only the tools this policy permits."""
        filtered = ToolRegistry()
        if base is None:
            return filtered
        for name in base.list_tools():
            tool = base.get(name)
            if tool is not None and self.permits(name, _required(tool)):
                filtered.register(tool)
        return filtered

    def approver(self, base: Approver | None = None) -> Approver:
        """Refuse calls outside the policy or over the call limit, then defer to *base*."""
        calls = 0

        async def approve(name: str, arguments: dict[str, Any], permissions: set) -> Any:
            nonlocal calls
            if not self.permits(name, permissions):
                return Denied(
                    f"'{name}' is not allowed for this agent by the '{self.profile}' "
                    "permission profile."
                )
            if name not in SCRATCH_TOOLS:
                calls += 1
                if calls > self.max_tool_calls:
                    return Denied(
                        f"the tool-call limit for this agent ({self.max_tool_calls}) is used up; "
                        "finish with what you have."
                    )
            return True if base is None else await base(name, arguments, permissions)

        return approve


def _required(tool: BaseTool) -> set[ToolPermission]:
    try:
        return set(tool.get_required_permissions())
    except Exception:
        return {P.TERMINAL_EXECUTE}  # unknown: treat as the most guarded kind
