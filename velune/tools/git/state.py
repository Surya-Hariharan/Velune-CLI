"""Git state tools — GitStatus, GitBranch.

Uses gitpython instead of raw subprocess calls.  PathGuard validates the
directory parameter so the tools cannot be pointed outside the workspace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from velune.execution.path_guard import PathGuard
from velune.tools.base.tool import BaseTool


def _open_repo(path: Path):  # type: ignore[return]
    try:
        import git

        return git.Repo(str(path), search_parent_directories=True)
    except Exception as exc:
        raise ValueError(f"Not a git repository: {path}") from exc


class GitStatus(BaseTool):
    """Tool for viewing git status."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_status"

    def get_description(self) -> str:
        return "View git repository status"

    async def execute(self, directory: str = ".") -> dict:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        repo = _open_repo(safe_root)

        def _fetch() -> dict:
            status: dict[str, list[str]] = {
                "modified": [],
                "added": [],
                "deleted": [],
                "untracked": [],
            }
            # Staged changes (index vs HEAD)
            try:
                for diff in repo.index.diff("HEAD"):
                    if diff.change_type == "M":
                        status["modified"].append(diff.b_path or diff.a_path)
                    elif diff.change_type == "A":
                        status["added"].append(diff.b_path)
                    elif diff.change_type == "D":
                        status["deleted"].append(diff.a_path)
            except Exception:
                pass
            # Unstaged changes (working tree vs index)
            try:
                for diff in repo.index.diff(None):
                    path = diff.a_path
                    if diff.change_type == "M" and path not in status["modified"]:
                        status["modified"].append(path)
                    elif diff.change_type == "D" and path not in status["deleted"]:
                        status["deleted"].append(path)
            except Exception:
                pass
            status["untracked"] = list(repo.untracked_files)
            return status

        return await asyncio.to_thread(_fetch)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Git repository directory"},
            },
        }


class GitBranch(BaseTool):
    """Tool for viewing git branches."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_branch"

    def get_description(self) -> str:
        return "View git branches"

    async def execute(self, directory: str = ".") -> dict:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        repo = _open_repo(safe_root)

        def _fetch() -> dict:
            try:
                current = repo.active_branch.name
            except TypeError:
                current = "(detached HEAD)"
            branches = [h.name for h in repo.heads]
            remote_branches = [ref.name for ref in repo.remote_refs] if repo.remotes else []
            return {
                "current": current,
                "all": branches + remote_branches,
            }

        return await asyncio.to_thread(_fetch)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Git repository directory"},
            },
        }
