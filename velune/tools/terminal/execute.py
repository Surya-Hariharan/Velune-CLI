from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.execution.sandbox import SubprocessSandbox

from velune.tools.base.tool import BaseTool


class ExecuteCommand(BaseTool):
    """Tool for executing terminal commands."""

    def __init__(self, sandbox: SubprocessSandbox | None = None, workspace_path: str | None = None):
        self._sandbox = sandbox
        self._workspace_path = workspace_path

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
        """Execute a command."""
        import asyncio
        from pathlib import Path

        from velune.core.errors.execution import SandboxError
        from velune.execution.command_spec import CommandSpec
        from velune.execution.sandbox import SubprocessSandbox

        workspace = Path(directory or self._workspace_path or Path.cwd())
        sandbox = self._sandbox or SubprocessSandbox(workspace)

        try:
            spec = CommandSpec.from_string(command, cwd=workspace, timeout=float(timeout))
        except SandboxError as e:
            sandbox.emit_rejection(command, str(e))
            raise e

        # Sandbox execution polls the subprocess with time.sleep — offload to a
        # worker thread so it never blocks the event loop.
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
