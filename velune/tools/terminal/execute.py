"""Terminal execution tools."""

from typing import Optional
from velune.tools.base.tool import BaseTool


class ExecuteCommand(BaseTool):
    """Tool for executing terminal commands."""

    def __init__(self, sandbox: Optional['SubprocessSandbox'] = None, workspace_path: Optional[str] = None):
        self._sandbox = sandbox
        self._workspace_path = workspace_path

    def get_name(self) -> str:
        return "execute_command"

    def get_description(self) -> str:
        return "Execute a terminal command"

    async def execute(
        self,
        command: str,
        directory: Optional[str] = None,
        timeout: int = 30,
    ) -> dict:
        """Execute a command."""
        from velune.execution.sandbox import SubprocessSandbox
        from pathlib import Path
        
        workspace = Path(directory or self._workspace_path or Path.cwd())
        sandbox = self._sandbox or SubprocessSandbox(workspace)
        
        # This now goes through SubprocessSandbox._is_safe_command() check
        result = sandbox.execute(command, cwd=Path(directory) if directory else None, timeout=float(timeout))
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
