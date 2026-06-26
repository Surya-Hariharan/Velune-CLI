"""Git history tools — GitLog, GitDiff, GitBlame.

All three tools use gitpython (``import git``) rather than raw subprocess so
that:
  - No shell is ever invoked.
  - File-path arguments are validated by ``PathGuard`` before being passed to
    git, preventing path-traversal escapes.
  - The ``--`` path separator is always used in commands that accept file paths,
    preventing user-supplied names from being misinterpreted as git flags or
    refs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from velune.execution.path_guard import PathGuard
from velune.tools.base.tool import BaseTool, ToolPermission


def _open_repo(path: Path):  # type: ignore[return]
    """Open a gitpython Repo, searching parent directories for .git."""
    try:
        import git

        return git.Repo(str(path), search_parent_directories=True)
    except Exception as exc:
        raise ValueError(f"Not a git repository: {path}") from exc


class GitLog(BaseTool):
    """Tool for viewing git commit history."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_log"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_READ}

    def get_description(self) -> str:
        return "View git commit history"

    async def execute(
        self,
        directory: str = ".",
        limit: int = 10,
    ) -> list[dict]:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        repo = _open_repo(safe_root)

        def _fetch() -> list[dict]:
            commits = []
            for commit in repo.iter_commits(max_count=max(1, int(limit))):
                commits.append(
                    {
                        "hash": commit.hexsha,
                        "author": commit.author.name,
                        "date": commit.authored_datetime.isoformat(),
                        "message": commit.message.strip(),
                    }
                )
            return commits

        return await asyncio.to_thread(_fetch)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Git repository directory"},
                "limit": {"type": "integer", "description": "Number of commits to show"},
            },
        }


class GitDiff(BaseTool):
    """Tool for viewing git diff."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_diff"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_READ}

    def get_description(self) -> str:
        return "View git diff"

    async def execute(
        self,
        directory: str = ".",
        file_path: str | None = None,
    ) -> str:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        repo = _open_repo(safe_root)

        def _fetch() -> str:
            if file_path:
                # Resolve and validate the file path, then make it relative to
                # the repo working directory so git can locate it.
                raw = Path(file_path)
                if not raw.is_absolute():
                    raw = safe_root / raw
                safe_file = guard.validate(raw)
                rel = str(safe_file.relative_to(Path(repo.working_dir).resolve()))
                # "--" prevents any path from being interpreted as a git ref.
                return repo.git.diff("--", rel)
            return repo.git.diff()

        return await asyncio.to_thread(_fetch)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Git repository directory"},
                "file_path": {"type": "string", "description": "Specific file to diff"},
            },
        }


class GitBlame(BaseTool):
    """Tool for viewing git blame."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_blame"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_READ}

    def get_description(self) -> str:
        return "View git blame for a file"

    async def execute(
        self,
        file_path: str,
        directory: str = ".",
    ) -> list[dict]:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        repo = _open_repo(safe_root)

        raw = Path(file_path)
        if not raw.is_absolute():
            raw = safe_root / raw
        safe_file = guard.validate(raw)
        rel = str(safe_file.relative_to(Path(repo.working_dir).resolve()))

        def _fetch() -> list[dict]:
            lines: list[dict] = []
            for commit, line_group in repo.blame("HEAD", rel):
                for content in line_group:
                    lines.append(
                        {
                            "commit": commit.hexsha,
                            "author": commit.author.name,
                            "date": commit.authored_datetime.isoformat(),
                            "content": content
                            if isinstance(content, str)
                            else content.decode("utf-8", errors="replace"),
                        }
                    )
            return lines

        return await asyncio.to_thread(_fetch)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File to blame"},
                "directory": {"type": "string", "description": "Git repository directory"},
            },
            "required": ["file_path"],
        }
