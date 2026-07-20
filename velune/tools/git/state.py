"""Git state tools — GitStatus, GitBranch.

Uses gitpython instead of raw subprocess calls.  PathGuard validates the
directory parameter so the tools cannot be pointed outside the workspace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from velune.execution.path_guard import PathGuard
from velune.tools.base.tool import BaseTool, ToolPermission


def _git_run(cwd: Path, *args: str) -> str:
    import subprocess

    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"Git error: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def _ensure_git_repo(path: Path) -> Path:
    try:
        _git_run(path, "rev-parse", "--is-inside-work-tree")
        return path
    except Exception as exc:
        raise ValueError(f"Not a git repository: {path}") from exc


class GitStatus(BaseTool):
    """Tool for viewing git status."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_status"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_READ}

    def get_description(self) -> str:
        return "View git repository status"

    async def execute(self, directory: str = ".") -> dict:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        _ensure_git_repo(safe_root)

        def _fetch() -> dict:
            status: dict[str, list[str]] = {
                "modified": [],
                "added": [],
                "deleted": [],
                "untracked": [],
            }
            try:
                out = _git_run(safe_root, "status", "--porcelain")
                for line in out.splitlines():
                    if len(line) < 4:
                        continue
                    xy = line[0:2]
                    path = line[3:].strip()
                    if path.startswith('"') and path.endswith('"'):
                        path = path[1:-1]
                    if xy == "??":
                        status["untracked"].append(path)
                    elif "A" in xy:
                        status["added"].append(path)
                    elif "D" in xy:
                        status["deleted"].append(path)
                    elif "M" in xy or "R" in xy or "C" in xy or "U" in xy:
                        status["modified"].append(path)
            except Exception:
                pass
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

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_READ}

    def get_description(self) -> str:
        return "View git branches"

    async def execute(self, directory: str = ".") -> dict:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        _ensure_git_repo(safe_root)

        def _fetch() -> dict:
            try:
                current = _git_run(safe_root, "symbolic-ref", "--short", "HEAD")
            except Exception:
                current = "(detached HEAD)"

            try:
                out = _git_run(safe_root, "branch", "-a", "--format=%(refname:short)")
                branches = [b for b in out.splitlines() if b]
            except Exception:
                branches = []

            return {
                "current": current,
                "all": branches,
            }

        return await asyncio.to_thread(_fetch)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Git repository directory"},
            },
        }
