from __future__ import annotations
from pathlib import Path
from typing import List
from velune.tools.base.tool import BaseTool


class TerminalHistory(BaseTool):
    """Tool for viewing terminal history."""

    def get_name(self) -> str:
        return "terminal_history"

    def get_description(self) -> str:
        return "View terminal command history"

    async def execute(
        self,
        limit: int = 50,
    ) -> list[str]:
        """Get terminal history."""
        history_file = Path.home() / ".bash_history"
        
        if not history_file.exists():
            history_file = Path.home() / ".zsh_history"
        
        if not history_file.exists():
            return []
        
        with open(history_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        
        # Get last N lines
        history = [line.strip() for line in lines[-limit:]]
        
        return history

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of history entries to return",
                },
            },
        }
