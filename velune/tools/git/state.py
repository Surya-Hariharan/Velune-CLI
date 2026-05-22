from __future__ import annotations
from pathlib import Path
from typing import List
from velune.tools.base.tool import BaseTool


class GitStatus(BaseTool):
    """Tool for viewing git status."""

    def get_name(self) -> str:
        return "git_status"

    def get_description(self) -> str:
        return "View git repository status"

    async def execute(self, directory: str = ".") -> dict:
        """Get git status."""
        import subprocess
        from pathlib import Path
        
        root_path = Path(directory)
        if not (root_path / ".git").exists():
            raise ValueError("Not a git repository")
        
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root_path,
            capture_output=True,
            text=True,
        )
        
        status = {
            "modified": [],
            "added": [],
            "deleted": [],
            "untracked": [],
        }
        
        for line in result.stdout.strip().split("\n"):
            if line:
                status_code = line[:2]
                file_path = line[3:]
                
                if status_code[0] == "M":
                    status["modified"].append(file_path)
                elif status_code[0] == "A":
                    status["added"].append(file_path)
                elif status_code[0] == "D":
                    status["deleted"].append(file_path)
                elif status_code[0] == "?":
                    status["untracked"].append(file_path)
        
        return status

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Git repository directory",
                },
            },
        }


class GitBranch(BaseTool):
    """Tool for viewing git branches."""

    def get_name(self) -> str:
        return "git_branch"

    def get_description(self) -> str:
        return "View git branches"

    async def execute(self, directory: str = ".") -> dict:
        """Get git branches."""
        import subprocess
        from pathlib import Path
        
        root_path = Path(directory)
        if not (root_path / ".git").exists():
            raise ValueError("Not a git repository")
        
        # Get current branch
        current_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root_path,
            capture_output=True,
            text=True,
        )
        current_branch = current_result.stdout.strip()
        
        # Get all branches
        all_result = subprocess.run(
            ["git", "branch", "-a"],
            cwd=root_path,
            capture_output=True,
            text=True,
        )
        
        branches = []
        for line in all_result.stdout.split("\n"):
            if line:
                branch_name = line.strip().replace("* ", "")
                branches.append(branch_name)
        
        return {
            "current": current_branch,
            "all": branches,
        }

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Git repository directory",
                },
            },
        }
