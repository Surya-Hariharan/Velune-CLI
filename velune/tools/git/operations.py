"""Git operation tools — GitCommit, GitCheckout.

Shells out to the real ``git`` binary via ``subprocess.run(["git", *args], ...)``
(no ``gitpython`` dependency is used, despite what an earlier version of this
docstring claimed). This still eliminates two classes of injection risk:
  - Shell injection: args are passed as a list, never through a shell, so
    there is no shell metacharacter interpretation to exploit.
  - Argument injection: ``_validate_ref_name`` rejects any branch name that
    starts with ``-`` before it reaches ``_git_run``, so a value like
    ``--upload-pack=...`` can't be smuggled in as a flag instead of a ref.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from velune.execution.path_guard import resolve_in_workspace
from velune.tools.base.tool import BaseTool, ToolPermission


def _git_run(cwd: Path, *args: str, strip: bool = True) -> str:
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
    return res.stdout.strip() if strip else res.stdout


def stageable_secret_files(repo: Path) -> list[str]:
    """Credential files that ``git add -A`` would stage in *repo* right now (repo-relative).

    A new, modified or renamed file whose name marks it as a secret (``.env``, a private key,
    ``.npmrc`` ...) and that no ignore rule excludes. ``git add -A`` followed by a commit would
    put it in history, where a later push publishes it; the caller asks the user first.
    Deletions are not listed: removing a secret file from the index exposes nothing.
    """
    from velune.permissions.boundary import is_secret_path

    out = _git_run(repo, "status", "--porcelain", "-z", "--untracked-files=all", strip=False)
    entries = out.split(chr(0))
    found: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            index += 1  # the next field is the path it was renamed or copied from
        if "D" in xy and "R" not in xy:
            continue
        if is_secret_path(Path(path)):
            found.append(path)
    return found


def command_stages_secrets(command: str, cwd: Path) -> list[str]:
    """Credential files a ``git add`` / ``git commit -a`` command line would stage (best effort).

    ``git add notes.py`` names exactly what it stages, so only those names are judged. Anything
    broader (``git add .``, ``-A``, ``-u``, a folder, a pattern, ``git commit -a``) is judged by
    what the repository would stage right now. Returns [] for every other command, and when
    *cwd* is not a repository (git then refuses the command itself).
    """
    from velune.core.argv import split_command
    from velune.permissions.boundary import is_secret_path
    from velune.permissions.commands import _git_command
    from velune.tools.safety import program_name, split_segments

    found: list[str] = []
    for segment in split_segments(command):
        try:
            tokens = split_command(segment)
        except ValueError:
            continue
        if not tokens:
            continue
        if program_name(tokens[0]).replace("\\", "/").rsplit("/", 1)[-1] != "git":
            continue
        sub, rest, _ = _git_command(tokens)
        operands = [a for a in rest if not a.startswith("-")]
        if sub == "add":
            explicit = bool(operands) and all(
                not any(ch in a for ch in "*?[") and (cwd / a).is_file() for a in operands
            )
            if explicit:
                found.extend(a for a in operands if is_secret_path(cwd / a))
                continue
        elif sub == "commit":
            stages_all = any(
                a == "--all" or (a.startswith("-") and not a.startswith("--") and "a" in a[1:])
                for a in rest
            )
            if not stages_all:
                continue
        else:
            continue
        try:
            found.extend(stageable_secret_files(cwd))
        except (RuntimeError, OSError):
            continue
    return found


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

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import Action, ActionType, Risk

        # ``add_all`` (the default) stages every change in the repository. If that includes a
        # credentials file, the commit is a secret-file change: confirmed by the user in every
        # mode, never covered by a session-wide grant, and named in the prompt.
        secrets: list[str] = []
        if args.get("add_all", True):
            try:
                repo = resolve_in_workspace(
                    args.get("directory") or ".", self.workspace, label=self.get_name()
                )
                secrets = stageable_secret_files(repo)
            except (ValueError, RuntimeError, OSError):
                secrets = []  # not a repository or a bad directory: execute() reports it
        reason = "create a git commit"
        if secrets:
            shown = ", ".join(secrets[:5]) + (
                f" and {len(secrets) - 5} more" if secrets[5:] else ""
            )
            reason = f"create a git commit that would add credential files to history: {shown}"
        return [
            Action(
                ActionType.GIT_WRITE,
                "git",
                reason,
                Risk.LOW,
                secret=bool(secrets),
                detail=str(args),
            )
        ]

    async def execute(
        self,
        message: str,
        directory: str = ".",
        add_all: bool = True,
    ) -> str:
        safe_root = resolve_in_workspace(directory, self.workspace, label=self.get_name())
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

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import Action, ActionType, Risk

        return [
            Action(
                ActionType.GIT_WRITE,
                "git",
                "switch branch or restore files",
                Risk.MEDIUM,
                detail=str(args),
            )
        ]

    async def execute(
        self,
        branch: str,
        directory: str = ".",
        create: bool = False,
    ) -> str:
        _validate_ref_name(branch, label="branch")
        safe_root = resolve_in_workspace(directory, self.workspace, label=self.get_name())
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
