"""Base tool protocol and execution contracts."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune._compat import StrEnum

if TYPE_CHECKING:
    from velune.hooks import HookDispatcher

_log = logging.getLogger("velune.tools.base")


class ToolPermission(StrEnum):
    """Permission boundaries enforced at tool execution time."""

    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    GIT_READ = "git.read"
    GIT_WRITE = "git.write"
    TERMINAL_EXECUTE = "terminal.execute"
    NETWORK_ACCESS = "network.access"


@dataclass(slots=True)
class ToolCallContext:
    """Execution context passed into policy-aware tool calls."""

    run_id: str
    actor: str
    workspace: Path | None = None
    permissions: set[ToolPermission] = field(default_factory=set)
    hook_dispatcher: HookDispatcher | None = field(default=None)
    session_id: str = ""
    # The execution-mode permission gate (velune.permissions.gate). Every
    # action a tool proposes is checked against it; with no gate, any
    # state-changing action is refused (fail closed).
    gate: Any = field(default=None)


class ToolBlockedError(RuntimeError):
    """Raised when a PreToolUse hook blocks a tool call."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"Tool '{tool_name}' blocked by hook: {reason}")
        self.tool_name = tool_name
        self.reason = reason


class BaseTool(ABC):
    """Abstract base class for tools.

    Subclasses implement ``execute(**kwargs)``; call ``guarded_execute()``
    instead when you want PreToolUse / PostToolUse hooks to fire automatically.
    """

    # Subclasses may override to declare a stable tool name used in hook
    # matching (e.g. "Bash", "Edit", "Write"). Defaults to get_name().
    HOOK_TOOL_NAME: str | None = None

    @abstractmethod
    def get_name(self) -> str:
        """Get the tool name."""

    @abstractmethod
    def get_description(self) -> str:
        """Get the tool description."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """Execute the tool. Subclasses implement this."""

    async def guarded_execute(
        self,
        ctx: ToolCallContext | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute the tool, firing Pre/PostToolUse hooks when a dispatcher is present.

        Args:
            ctx:      Optional execution context carrying a HookDispatcher.
            **kwargs: Tool-specific arguments forwarded to ``execute``.

        Returns:
            Tool result (type depends on the concrete tool).

        Raises:
            ToolBlockedError: When a PreToolUse hook returns ``decision: block``.
        """
        tool_name = self.HOOK_TOOL_NAME or self.get_name()
        dispatcher = ctx.hook_dispatcher if ctx else None
        session_id = ctx.session_id if ctx else ""

        # ── PreToolUse hook ──────────────────────────────────────────
        if dispatcher is not None:
            try:
                pre_result = await dispatcher.dispatch_pre_tool_use(
                    tool_name=tool_name,
                    tool_input=kwargs,
                    session_id=session_id,
                )
                if pre_result.blocked:
                    raise ToolBlockedError(tool_name, pre_result.block_reason)
                if pre_result.system_message:
                    _log.info(
                        "[hook] PreToolUse notice for %s: %s", tool_name, pre_result.system_message
                    )
            except ToolBlockedError:
                raise
            except Exception as exc:
                _log.debug("PreToolUse hook error (non-fatal): %s", exc)

        # ── Execute ──────────────────────────────────────────────────
        result = await self.execute(**kwargs)

        # ── PostToolUse hook ─────────────────────────────────────────
        if dispatcher is not None:
            try:
                await dispatcher.dispatch_post_tool_use(
                    tool_name=tool_name,
                    tool_input=kwargs,
                    tool_result=result,
                    session_id=session_id,
                )
            except Exception as exc:
                _log.debug("PostToolUse hook error (non-fatal): %s", exc)

        return result

    def get_schema(self) -> dict[str, Any]:
        """Get the tool's parameter schema."""
        return {}

    def get_required_permissions(self) -> set[ToolPermission]:
        """Permissions required to execute this tool."""
        return set()

    def validate_input(self, payload: dict[str, Any]) -> None:
        """Validate tool input before execution."""
        return None

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        """Describe what this call would do, as :class:`velune.permissions.Action`s.

        The permission gate decides on these *before* the tool runs. Tools
        that change state override this with precise targets (the file, the
        command). This default derives a conservative action from the declared
        permissions, so a tool that forgets to override is still gated rather
        than silently allowed.
        """
        from velune.permissions.actions import Action, ActionType, Risk

        perms = self.get_required_permissions()
        name = self.get_name()
        actions: list[Any] = []
        if ToolPermission.FILESYSTEM_WRITE in perms:
            actions.append(Action(ActionType.MODIFY_FILE, f"<{name}>", f"{name} changes files"))
        if ToolPermission.GIT_WRITE in perms:
            actions.append(Action(ActionType.GIT_WRITE, f"<{name}>", f"{name} changes git state"))
        if ToolPermission.TERMINAL_EXECUTE in perms:
            actions.append(
                Action(ActionType.RUN_COMMAND, f"<{name}>", f"{name} runs commands", Risk.MEDIUM)
            )
        if ToolPermission.NETWORK_ACCESS in perms:
            actions.append(Action(ActionType.NETWORK, f"<{name}>", f"{name} uses the network"))
        return actions or [Action(ActionType.READ, f"<{name}>", "read-only")]


class ToolPermissionError(PermissionError):
    """Raised when a tool's required permissions are not granted by the context."""

    def __init__(self, tool_name: str, missing: set[ToolPermission]) -> None:
        scopes = ", ".join(sorted(p.value for p in missing))
        super().__init__(f"Tool '{tool_name}' denied — missing permission(s): {scopes}")
        self.tool_name = tool_name
        self.missing = missing


async def authorize_and_execute(
    tool: BaseTool,
    ctx: ToolCallContext | None,
    /,
    **kwargs: Any,
) -> Any:
    """Single enforced entry point for running a tool.

    Enforces the declared permission model — comparing the tool's
    ``get_required_permissions()`` against the scopes granted in *ctx* — and then
    runs the tool through :meth:`BaseTool.guarded_execute` so PreToolUse /
    PostToolUse hooks fire. Callers that previously invoked ``tool.execute(...)``
    directly bypassed both the permission check and the hooks.

    Raises:
        ToolPermissionError: when a required scope is not granted.
        ToolBlockedError:     when a PreToolUse hook blocks the call.
    """
    required = tool.get_required_permissions()
    granted = ctx.permissions if ctx is not None else set()
    missing = required - granted
    if missing:
        raise ToolPermissionError(tool.get_name(), missing)

    # Execution-mode policy: the tool describes its actions, the gate decides
    # (ALLOW / DENY / ASK the user) before anything runs.
    from velune.permissions.boundary import Boundary
    from velune.permissions.gate import (
        ActionDeniedError,
        reset_active_boundary,
        set_active_boundary,
    )

    gate = ctx.gate if ctx is not None else None
    if gate is not None:
        boundary = gate.boundary
    else:
        workspace = (ctx.workspace if ctx is not None else None) or Path.cwd()
        boundary = Boundary(Path(workspace))
    actions = tool.describe_actions(dict(kwargs), boundary)
    if gate is None:
        if any(a.mutating or a.secret or a.outside_workspace for a in actions):
            raise ActionDeniedError(
                tool.get_name(), "no permission gate is active for this call (fail closed)"
            )
    else:
        await gate.check(tool.get_name(), actions)

    token = set_active_boundary(gate.boundary if gate is not None else None)
    try:
        return await tool.guarded_execute(ctx, **kwargs)
    finally:
        reset_active_boundary(token)
