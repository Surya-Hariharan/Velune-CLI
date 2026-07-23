"""Git history tools — GitLog, GitDiff, GitBlame.

All three tools shell out to the real ``git`` binary via
``subprocess.run(["git", *args], ...)`` (no ``gitpython`` dependency is used,
despite what an earlier version of this docstring claimed):
  - No shell is ever invoked — args are passed as a list, not a shell string.
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
        _ensure_git_repo(safe_root)

        def _fetch() -> list[dict]:
            commits = []
            try:
                out = _git_run(
                    safe_root,
                    "log",
                    f"-n{max(1, int(limit))}",
                    "--format=%H%n%an%n%aI%n%s%n---VELUNE---",
                )
                if out:
                    for block in out.split("---VELUNE---"):
                        lines = block.strip().split("\n")
                        if len(lines) >= 4:
                            commits.append(
                                {
                                    "hash": lines[0],
                                    "author": lines[1],
                                    "date": lines[2],
                                    "message": "\n".join(lines[3:]).strip(),
                                }
                            )
            except Exception:
                pass
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
        _ensure_git_repo(safe_root)

        def _fetch() -> str:
            try:
                if file_path:
                    raw = Path(file_path)
                    if not raw.is_absolute():
                        raw = safe_root / raw
                    safe_file = guard.validate(raw)
                    rel = str(safe_file.relative_to(safe_root))
                    return _git_run(safe_root, "diff", "--", rel)
                return _git_run(safe_root, "diff")
            except Exception as e:
                return str(e)

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
        _ensure_git_repo(safe_root)

        raw = Path(file_path)
        if not raw.is_absolute():
            raw = safe_root / raw
        safe_file = guard.validate(raw)
        rel = str(safe_file.relative_to(safe_root))

        def _fetch() -> list[dict]:
            lines: list[dict] = []
            try:
                out = _git_run(safe_root, "blame", "-p", "HEAD", "--", rel)
            except Exception:
                return lines

            current_commit = {}
            for line in out.splitlines():
                if not line:
                    continue
                if line.startswith("\t"):
                    content = line[1:]
                    lines.append(
                        {
                            "commit": current_commit.get("hash", ""),
                            "author": current_commit.get("author", ""),
                            "date": current_commit.get("date", ""),
                            "content": content,
                        }
                    )
                    # Keep hash, author, date if subsequent lines belong to the same commit
                else:
                    parts = line.split(" ", 1)
                    key = parts[0]
                    if len(key) == 40:  # commit hash
                        current_commit["hash"] = key
                    elif key == "author":
                        current_commit["author"] = parts[1] if len(parts) > 1 else ""
                    elif key == "author-time":
                        import datetime

                        try:
                            ts = int(parts[1])
                            current_commit["date"] = datetime.datetime.fromtimestamp(
                                ts, tz=datetime.timezone.utc
                            ).isoformat()
                        except Exception:
                            current_commit["date"] = ""
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
