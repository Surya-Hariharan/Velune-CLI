"""Terminal history awareness."""

from pathlib import Path
from typing import list


class TerminalAwareness:
    """Provides awareness of terminal history."""

    def __init__(self):
        pass

    def get_recent_commands(self, limit: int = 10) -> list[str]:
        """Get recent terminal commands."""
        history_file = Path.home() / ".bash_history"
        
        if not history_file.exists():
            history_file = Path.home() / ".zsh_history"
        
        if not history_file.exists():
            return []
        
        try:
            with open(history_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            
            # Get last N lines
            history = [line.strip() for line in lines[-limit:]]
            return history
        except Exception:
            return []
