"""Git history tools."""

from pathlib import Path
from typing import list, Optional
from velune.tools.base.tool import BaseTool


class GitLog(BaseTool):
    """Tool for viewing git commit history."""

    def get_name(self) -> str:
        return "git_log"

    def get_description(self) -> str:
        return "View git commit history"

    async def execute(
        self,
        directory: str = ".",
        limit: int = 10,
    ) -> list[dict]:
        """Get git commit history."""
        import subprocess
        from pathlib import Path
        
        root_path = Path(directory)
        if not (root_path / ".git").exists():
            raise ValueError("Not a git repository")
        
        result = subprocess.run(
            ["git", "log", f"-{limit}", "--pretty=format:%H|%an|%ad|%s", "--date=iso"],
            cwd=root_path,
            capture_output=True,
            text=True,
        )
        
        commits = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("|", 3)
                commits.append({
                    "hash": parts[0],
                    "author": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                })
        
        return commits

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Git repository directory",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of commits to show",
                },
            },
        }


class GitDiff(BaseTool):
    """Tool for viewing git diff."""

    def get_name(self) -> str:
        return "git_diff"

    def get_description(self) -> str:
        return "View git diff"

    async def execute(
        self,
        directory: str = ".",
        file_path: Optional[str] = None,
    ) -> str:
        """Get git diff."""
        import subprocess
        from pathlib import Path
        
        root_path = Path(directory)
        if not (root_path / ".git").exists():
            raise ValueError("Not a git repository")
        
        cmd = ["git", "diff"]
        if file_path:
            cmd.append(file_path)
        
        result = subprocess.run(
            cmd,
            cwd=root_path,
            capture_output=True,
            text=True,
        )
        
        return result.stdout

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Git repository directory",
                },
                "file_path": {
                    "type": "string",
                    "description": "Specific file to diff",
                },
            },
        }


class GitBlame(BaseTool):
    """Tool for viewing git blame."""

    def get_name(self) -> str:
        return "git_blame"

    def get_description(self) -> str:
        return "View git blame for a file"

    async def execute(
        self,
        file_path: str,
        directory: str = ".",
    ) -> list[dict]:
        """Get git blame."""
        import subprocess
        from pathlib import Path
        
        root_path = Path(directory)
        if not (root_path / ".git").exists():
            raise ValueError("Not a git repository")
        
        result = subprocess.run(
            ["git", "blame", file_path],
            cwd=root_path,
            capture_output=True,
            text=True,
        )
        
        lines = []
        for line in result.stdout.split("\n"):
            if line:
                parts = line.split(None, 3)
                lines.append({
                    "commit": parts[0],
                    "author": parts[1],
                    "date": parts[2],
                    "content": parts[3] if len(parts) > 3 else "",
                })
        
        return lines

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "File to blame",
                },
                "directory": {
                    "type": "string",
                    "description": "Git repository directory",
                },
            },
            "required": ["file_path"],
        }
