"""Git operation tools."""

from pathlib import Path

from velune.tools.base.tool import BaseTool


class GitCommit(BaseTool):
    """Tool for committing changes."""

    def get_name(self) -> str:
        return "git_commit"

    def get_description(self) -> str:
        return "Commit changes to git"

    async def execute(
        self,
        message: str,
        directory: str = ".",
        add_all: bool = True,
    ) -> str:
        """Commit changes."""
        import subprocess

        root_path = Path(directory)
        if not (root_path / ".git").exists():
            raise ValueError("Not a git repository")

        if add_all:
            subprocess.run(["git", "add", "."], cwd=root_path, check=True)

        subprocess.run(["git", "commit", "-m", message], cwd=root_path, check=True)

        return f"Committed: {message}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message",
                },
                "directory": {
                    "type": "string",
                    "description": "Git repository directory",
                },
                "add_all": {
                    "type": "boolean",
                    "description": "Add all changes before committing",
                },
            },
            "required": ["message"],
        }


class GitCheckout(BaseTool):
    """Tool for checking out branches."""

    def get_name(self) -> str:
        return "git_checkout"

    def get_description(self) -> str:
        return "Checkout a git branch"

    async def execute(
        self,
        branch: str,
        directory: str = ".",
        create: bool = False,
    ) -> str:
        """Checkout a branch."""
        import subprocess

        root_path = Path(directory)
        if not (root_path / ".git").exists():
            raise ValueError("Not a git repository")

        cmd = ["git", "checkout"]
        if create:
            cmd.append("-b")
        cmd.append(branch)

        subprocess.run(cmd, cwd=root_path, check=True)

        return f"Checked out: {branch}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch name",
                },
                "directory": {
                    "type": "string",
                    "description": "Git repository directory",
                },
                "create": {
                    "type": "boolean",
                    "description": "Create branch if it doesn't exist",
                },
            },
            "required": ["branch"],
        }
