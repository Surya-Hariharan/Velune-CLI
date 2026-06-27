"""Git operation tools — GitCommit, GitCheckout.

Uses gitpython's high-level API instead of raw subprocess.  This eliminates
two classes of injection risk:
  - Shell injection (subprocess with a shell argument is not used; gitpython handles this).
  - Argument injection: branch names are looked up by key in the Repo heads
    dict rather than spliced into a command string, so ``--detach`` or similar
    flag-like inputs cannot influence git's option parsing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from velune.execution.path_guard import PathGuard
from velune.tools.base.tool import BaseTool, ToolPermission


def _git_run(cwd: Path, *args: str) -> str:
    import subprocess

    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Git error: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def _ensure_git_repo(path: Path) -> Path:
    try:
        _git_run(path, "rev-parse", "--is-inside-work-tree")
        return path
    except Exception as exc:
        raise ValueError(f"Not a git repository: {path}") from exc


def _validate_ref_name(name: str, label: str = "name") -> None:
    """Reject ref names that look like git option flags."""
    if name.startswith("-"):
        raise ValueError(f"Invalid git {label} '{name}': names must not start with '-'.")


class GitCommit(BaseTool):
    """Tool for committing changes."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_commit"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_WRITE}

    def get_description(self) -> str:
        return "Commit changes to git"

    async def execute(
        self,
        message: str,
        directory: str = ".",
        add_all: bool = True,
    ) -> str:
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        _ensure_git_repo(safe_root)

        def _do_commit() -> str:
            if add_all:
                _git_run(safe_root, "add", "-A")
            _git_run(safe_root, "commit", "-m", message)

            # Get the new commit hash
            new_sha = _git_run(safe_root, "rev-parse", "--short", "HEAD")
            return f"Committed: {message} ({new_sha})"

        return await asyncio.to_thread(_do_commit)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
                "directory": {"type": "string", "description": "Git repository directory"},
                "add_all": {"type": "boolean", "description": "Add all changes before committing"},
            },
            "required": ["message"],
        }


class GitCheckout(BaseTool):
    """Tool for checking out branches."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_checkout"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_WRITE}

    def get_description(self) -> str:
        return "Checkout a git branch"

    async def execute(
        self,
        branch: str,
        directory: str = ".",
        create: bool = False,
    ) -> str:
        _validate_ref_name(branch, label="branch")
        guard = PathGuard(self.workspace)
        safe_root = guard.validate(directory)
        _ensure_git_repo(safe_root)

        def _do_checkout() -> str:
            if create:
                _git_run(safe_root, "checkout", "-b", branch)
            else:
                # verify branch exists
                _git_run(safe_root, "rev-parse", "--verify", branch)
                _git_run(safe_root, "checkout", branch)
            return f"Checked out: {branch}"

        return await asyncio.to_thread(_do_checkout)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch name"},
                "directory": {"type": "string", "description": "Git repository directory"},
                "create": {"type": "boolean", "description": "Create branch if it doesn't exist"},
            },
            "required": ["branch"],
        }
