from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.execution.sandbox import SubprocessSandbox

from velune.tools.base.tool import BaseTool
from velune.tools.safety import ApprovalMode, classify_command


class ExecuteCommand(BaseTool):
    """Tool for executing terminal commands.

    Respects the session-level ApprovalMode:
      SAFE   — runs without confirmation (if command is also read-only).
      ASK    — raises PermissionError so the REPL can prompt the user.
      BLOCK  — always raises PermissionError.
    """

    def __init__(
        self,
        sandbox: SubprocessSandbox | None = None,
        workspace_path: str | None = None,
        approval_mode: ApprovalMode = ApprovalMode.ASK,
    ):
        self._sandbox = sandbox
        self._workspace_path = workspace_path
        self.approval_mode = approval_mode

    def get_name(self) -> str:
        return "execute_command"

    def get_description(self) -> str:
        return "Execute a terminal command"

    async def execute(
        self,
        command: str,
        directory: str | None = None,
        timeout: int = 30,
    ) -> dict:
        """Execute a command after applying the current ApprovalMode gate."""
        import asyncio
        from pathlib import Path

        from velune.core.errors.execution import SandboxError
        from velune.execution.command_spec import CommandSpec
        from velune.execution.sandbox import SubprocessSandbox

        # --- ApprovalMode gate -------------------------------------------
        verdict = classify_command(command)

        if self.approval_mode == ApprovalMode.BLOCK:
            raise PermissionError(
                f"Command execution is blocked (approval mode: block): {command!r}"
            )

        if verdict.mode == ApprovalMode.BLOCK:
            raise PermissionError(f"Command refused — {verdict.reason}: {command!r}")

        if self.approval_mode == ApprovalMode.ASK and verdict.mode != ApprovalMode.SAFE:
            # Signal to the REPL that this command needs user confirmation.
            # The REPL catches PermissionError with this prefix and shows a prompt.
            raise PermissionError(f"__approval_required__:{command}")
        # -----------------------------------------------------------------

        workspace = Path(directory or self._workspace_path or Path.cwd())
        sandbox = self._sandbox or SubprocessSandbox(workspace)

        try:
            spec = CommandSpec.from_string(command, cwd=workspace, timeout=float(timeout))
        except SandboxError as e:
            sandbox.emit_rejection(command, str(e))
            raise e

        result = await asyncio.to_thread(sandbox.execute, spec)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
        }

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to execute",
                },
                "directory": {
                    "type": "string",
                    "description": "Working directory",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Command timeout in seconds",
                },
            },
            "required": ["command"],
        }
