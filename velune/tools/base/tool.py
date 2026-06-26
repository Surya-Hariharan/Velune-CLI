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
