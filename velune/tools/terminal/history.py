from __future__ import annotations

import os
import sys
from pathlib import Path

from velune.tools.base.tool import BaseTool


def _candidate_history_files() -> list[Path]:
    """Well-known shell history file locations for this platform.

    bash/zsh cover POSIX shells (also present under WSL/Git Bash on Windows).
    PowerShell's PSReadLine module is the default interactive shell on
    Windows and persists its own history file under %APPDATA%, independent
    of any POSIX shell history — checked there so the tool isn't silently a
    no-op for Windows users who never touch bash/zsh.
    """
    candidates = [Path.home() / ".bash_history", Path.home() / ".zsh_history"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(
                Path(appdata)
                / "Microsoft"
                / "Windows"
                / "PowerShell"
                / "PSReadLine"
                / "ConsoleHost_history.txt"
            )
    return candidates


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
        history_file = next((f for f in _candidate_history_files() if f.exists()), None)

        if history_file is None:
            return []

        with open(history_file, encoding="utf-8", errors="ignore") as f:
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
