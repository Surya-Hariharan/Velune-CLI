"""Git state awareness."""

import subprocess
from pathlib import Path


class GitAwareness:
    """Provides awareness of git state."""

    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path

    def get_branch(self) -> str | None:
        """Get current git branch."""
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def get_commit_hash(self) -> str | None:
        """Get current commit hash."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def is_dirty(self) -> bool:
        """Check if working directory is dirty."""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return bool(result.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def has_uncommitted_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        return self.is_dirty()
