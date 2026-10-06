"""Workspace boundary and secret-path detection for proposed actions.

The workspace root always comes from the session, never from tool arguments:
a model-supplied path or ``directory`` can *name* a location, but whether that
location is inside the workspace is decided here. Paths outside it are not
forbidden outright; they become ``outside_workspace`` actions that the policy
turns into an explicit question (allow once / for this task / deny).
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

# Files that hold credentials. Reading or changing them is always high-risk.
_SECRET_NAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "secrets.json",
    "credentials.json",
    ".netrc",
    ".pypirc",
    ".npmrc",
)
# Directories whose contents are private by nature.
_SECRET_DIR_NAMES = frozenset({".ssh", ".gnupg", ".aws", ".secrets"})
# A few allowed template files that only *look* like secrets.
_NOT_SECRET = frozenset({".env.example", ".env.sample", ".env.template"})


def is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name in _NOT_SECRET:
        return False
    if any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_NAME_PATTERNS):
        return True
    return any(part.lower() in _SECRET_DIR_NAMES for part in path.parts)


@dataclass
class Boundary:
    """The workspace root plus any extra roots the user granted for this task."""

    workspace_root: Path
    extra_roots: set[Path] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).resolve()

    def resolve(self, raw: str | Path) -> Path:
        """Anchor relative paths to the workspace (not the process CWD)."""
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        return candidate.resolve()

    def _within(self, path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def inside(self, path: Path) -> bool:
        """True if *path* is in the workspace or a root granted for this task."""
        return self._within(path, self.workspace_root) or any(
            self._within(path, root) for root in self.extra_roots
        )

    @staticmethod
    def root_for(path: Path) -> Path:
        """The directory a grant for *path* covers (the path itself if it's a folder)."""
        return (path if path.is_dir() or not path.suffix else path.parent).resolve()

    def grant(self, path: Path) -> None:
        """Allow a directory outside the workspace for the rest of the task."""
        self.extra_roots.add(self.root_for(path))

    def with_roots(self, roots: list[Path]) -> Boundary:
        """A copy that also admits *roots* (used for one approved call)."""
        return Boundary(self.workspace_root, set(self.extra_roots) | set(roots))

    def clear_grants(self) -> None:
        self.extra_roots.clear()


def path_action(
    action_type,
    raw: str | Path,
    boundary: Boundary,
    reason: str = "",
    risk=None,
    detail: str = "",
):
    """Build an :class:`Action` for a path argument, with boundary/secret flags set."""
    from velune.permissions.actions import Action, Risk

    resolved = boundary.resolve(raw)
    secret = is_secret_path(resolved)
    outside = not boundary.inside(resolved)
    if risk is None:
        risk = Risk.HIGH if secret else Risk.LOW
    return Action(
        action_type,
        str(resolved),
        reason=reason,
        risk=risk,
        outside_workspace=outside,
        secret=secret,
        detail=detail,
    )


_DRIVE_OR_HOME = re.compile(r"^(~|[A-Za-z]:[\\/])")
_PARENT_SEGMENT = re.compile(r"(^|[\\/])\.\.([\\/]|$)")


def _path_like(token: str) -> bool:
    if "://" in token:
        return False
    if _DRIVE_OR_HOME.match(token) or _PARENT_SEGMENT.search(token):
        return True
    if token.startswith("\\"):
        return True
    if token.startswith("/"):
        # On Windows a lone "/x" is a switch (dir /s, robocopy /MIR), not a path.
        return os.name != "nt" or token.count("/") > 1
    return False


def command_paths_outside(command: str, boundary: Boundary) -> list[Path]:
    """Paths named in a command line that lie outside the boundary.

    The first token (the executable) is skipped; ``--opt=value`` is checked by
    its value. Best effort: it catches what a command *names*, not what a
    program decides to touch on its own.
    """
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    outside: list[Path] = []
    for raw in tokens[1:]:
        token = raw.strip("\"'")
        if token.startswith("-") and "=" in token:
            token = token.split("=", 1)[1].strip("\"'")
        if not token or not _path_like(token):
            continue
        try:
            resolved = boundary.resolve(token)
        except (OSError, ValueError, RuntimeError):
            continue
        if not boundary.inside(resolved):
            outside.append(resolved)
    return outside
