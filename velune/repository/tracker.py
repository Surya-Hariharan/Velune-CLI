"""Git tracking, commit history, and code blame metrics."""

import subprocess
from pathlib import Path


class GitTracker:
    """Direct Git integration for capturing branch topology, blames, and commit volatility."""

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()
        self.is_git = (self.root_path / ".git").exists()

    def get_active_branch(self) -> str:
        """Returns the name of the currently checked out Git branch."""
        if not self.is_git:
            return "non-git"
        try:
            res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            return res.strip()
        except Exception:
            return "unknown"

    def get_uncommitted_changes(self) -> list[str]:
        """Lists all unstaged, staged, or untracked changes in the workspace."""
        if not self.is_git:
            return []
        try:
            res = self._run_git(["status", "--porcelain"])
            changes = []
            for line in res.splitlines():
                if len(line) > 3:
                    # Status code is first two characters, then space, then file path
                    changes.append(line[3:].strip())
            return changes
        except Exception:
            return []

    def get_recent_commits(self, limit: int = 10) -> list[dict[str, str]]:
        """Retrieves a list of recent commits with metadata."""
        if not self.is_git:
            return []
        try:
            # Format: hash | author | date | subject
            res = self._run_git(["log", "-n", str(limit), "--pretty=format:%H|%an|%ad|%s", "--date=short"])
            commits = []
            for line in res.splitlines():
                parts = line.split("|")
                if len(parts) >= 4:
                    commits.append({
                        "hash": parts[0],
                        "author": parts[1],
                        "date": parts[2],
                        "subject": parts[3]
                    })
            return commits
        except Exception:
            return []

    def get_file_volatility(self, file_path: str, days: int = 90) -> int:
        """Calculates commit volatility (number of times modified in Git) over a period."""
        if not self.is_git:
            return 0
        try:
            # Count commit entries modifying this file
            res = self._run_git(["log", f"--since={days} days ago", "--oneline", "--", file_path])
            return len(res.splitlines())
        except Exception:
            return 0

    def get_blame(self, file_path: str) -> list[dict[str, str]]:
        """Parses git blame details to index code line ownership and recency."""
        if not self.is_git:
            return []
        try:
            # git blame --porcelain file
            res = self._run_git(["blame", "--porcelain", file_path])
            blames = []
            commit_data: dict[str, dict[str, str]] = {}
            lines = res.splitlines()

            i = 0
            while i < len(lines):
                line = lines[i]
                parts = line.split()
                if not parts:
                    i += 1
                    continue

                sha = parts[0]
                if sha not in commit_data:
                    # Parse commit info block
                    author = "unknown"
                    date = "unknown"
                    j = i + 1
                    while j < len(lines) and not lines[j].startswith("\t"):
                        if lines[j].startswith("author "):
                            author = lines[j][7:]
                        elif lines[j].startswith("author-time "):
                            date = lines[j][12:]
                        j += 1
                    commit_data[sha] = {"author": author, "date": date}

                # Find line contents
                j = i + 1
                while j < len(lines) and not lines[j].startswith("\t"):
                    j += 1
                if j < len(lines) and lines[j].startswith("\t"):
                    content = lines[j][1:]
                    blames.append({
                        "commit": sha,
                        "author": commit_data[sha]["author"],
                        "date": commit_data[sha]["date"],
                        "content": content
                    })
                i = j + 1
            return blames
        except Exception:
            return []

    def create_stash(self, name: str = "velune-snapshot") -> bool:
        """Stashes current uncommitted modifications to prepare for validation or rollback."""
        if not self.is_git:
            return False
        try:
            self._run_git(["stash", "push", "-m", name, "--include-untracked"])
            return True
        except Exception:
            return False

    def pop_stash(self) -> bool:
        """Pops the last stashed state, restoring uncommitted changes."""
        if not self.is_git:
            return False
        try:
            self._run_git(["stash", "pop"])
            return True
        except Exception:
            return False

    def apply_stash(self) -> bool:
        """Applies the last stashed state, keeping it in the stash list."""
        if not self.is_git:
            return False
        try:
            self._run_git(["stash", "apply"])
            return True
        except Exception:
            return False

    def drop_stash(self) -> bool:
        """Drops the last stashed state from the stash list."""
        if not self.is_git:
            return False
        try:
            self._run_git(["stash", "drop"])
            return True
        except Exception:
            return False

    def _run_git(self, args: list[str]) -> str:
        """Helper to safely execute git subprocess commands in the repository root."""
        cmd = ["git"] + args
        res = subprocess.run(
            cmd,
            cwd=self.root_path,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            errors="ignore"
        )
        return res.stdout

    async def _run_git_async(self, args: list[str]) -> str:
        """Async version of _run_git using asyncio.to_thread."""
        import asyncio
        import functools
        cmd = ["git"] + args
        try:
            result = await asyncio.to_thread(
                functools.partial(
                    subprocess.run,
                    cmd,
                    cwd=self.root_path,
                    capture_output=True,
                    text=True,
                    check=True,
                    encoding="utf-8",
                    errors="ignore",
                )
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Git command failed: {' '.join(cmd)}: {e.stderr}") from e

    def get_all_file_volatility(self, days: int = 90) -> dict[str, int]:
        """Get commit counts for ALL files in a single git log call.
        
        Returns:
            dict mapping relative file path → commit count in last {days} days.
            Empty dict if not a git repo.
        """
        if not self.is_git:
            return {}
        try:
            result = self._run_git([
                "log",
                f"--since={days} days ago",
                "--pretty=format:",
                "--name-only",
            ])

            counts: dict[str, int] = {}
            for line in result.splitlines():
                line = line.strip()
                if line:  # Skip empty lines (between commits)
                    # Normalize paths to use forward-slashes (consistent across all platforms)
                    normalized_line = line.replace("\\", "/")
                    counts[normalized_line] = counts.get(normalized_line, 0) + 1
            return counts
        except subprocess.CalledProcessError as e:
            import logging
            logging.getLogger("velune.repository.tracker").warning(
                "Git log batch volatility failed with process error: %s", e
            )
            return {}
        except Exception as e:
            import logging
            logging.getLogger("velune.repository.tracker").error(
                "Unexpected error in get_all_file_volatility: %s", e
            )
            return {}
