"""Terminal execution tools."""

from typing import Optional
from velune.tools.base.tool import BaseTool


class ExecuteCommand(BaseTool):
    """Tool for executing terminal commands."""

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
        import subprocess
        from pathlib import Path
        
        cwd = Path(directory) if directory else None
        
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
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
