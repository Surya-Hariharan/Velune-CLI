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


_GIT_REASON = "changes git internals (.git/ — hooks and config there can run code)"
_HOOKS_REASON = "changes Velune hook configuration (hooks run commands)"
_VELUNE_DIR_REASON = (
    "changes the Velune configuration folder (.velune/ — the hook configuration inside it "
    "runs commands)"
)
_TOML_REASON = "changes Velune configuration (it can define hooks that run commands)"
_LINK_NOTE = " (this path is, or links to, a protected location)"

# A folder move is checked by looking at what it would install at the destination. The walk is
# bounded; a tree too big to inspect is not assumed to be harmless.
MAX_MOVE_SCAN_ENTRIES = 20_000
MAX_MOVE_SCAN_DEPTH = 64


def protected_reason(path: Path) -> str:
    """Why changing *path* is high-risk even inside the workspace ("" if it is not).

    These files can make other programs run code: git runs ``.git/hooks/*`` and
    honours ``.git/config`` (aliases, ``core.fsmonitor``, ``core.hooksPath``);
    Velune runs the commands in its hook configuration. Changing them is not
    forbidden — it is confirmed by the user in every mode, and never covered by a
    session-wide "always allow". Reading them is ordinary.

    This judges the *name* of the path. The ``.velune`` folder itself is protected, not
    only ``hooks.json`` inside it: creating, moving, replacing or deleting the folder
    changes which hook file is active. Links are handled by :func:`protected_for`.
    """
    parts = [part.lower() for part in path.parts]
    if ".git" in parts:
        return _GIT_REASON
    name = parts[-1] if parts else ""
    if name == ".velune":
        return _VELUNE_DIR_REASON
    if name == "hooks.json" and len(parts) > 1 and parts[-2] == ".velune":
        return _HOOKS_REASON
    if name == "velune.toml":
        return _TOML_REASON
    return ""


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path


def _same_place(a: Path, b: Path) -> bool:
    """True if two paths are the same filesystem object (string match or real identity)."""
    if a == b or str(a).casefold() == str(b).casefold():
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _canonical_protected(root: Path) -> list[tuple[Path, bool, str]]:
    """The protected locations under *root*, resolved through any links they are made of.

    ``(location, whole_tree, reason)``: for a whole-tree entry everything below it counts.
    """
    return [
        (_safe_resolve(root / ".git"), True, _GIT_REASON),
        (_safe_resolve(root / ".velune"), False, _VELUNE_DIR_REASON),
        (_safe_resolve(root / ".velune" / "hooks.json"), False, _HOOKS_REASON),
        (_safe_resolve(root / "velune.toml"), False, _TOML_REASON),
    ]


def _alias_reason(resolved: Path, roots: list[Path]) -> str:
    """Is *resolved* the very place a protected location points at (whatever it is called)?

    Catches a protected name that is a link to somewhere ordinary (``.velune`` -> ``cfg``
    makes ``cfg/hooks.json`` the live hook file) and an ordinary name that links to a
    protected target. Compares real identity, not just text.
    """
    for root in roots:
        for location, whole_tree, reason in _canonical_protected(root):
            if _same_place(resolved, location):
                return reason + _LINK_NOTE
            if whole_tree and any(_same_place(parent, location) for parent in resolved.parents):
                return reason + _LINK_NOTE
    return ""


def protected_for(requested: Path, resolved: Path, roots: list[Path]) -> str:
    """Protection verdict for a path argument, from every identity it has.

    *requested* is the path as asked for (``..`` collapsed, links **not** followed) and
    *resolved* is where it really leads. Either may be protected, and so may the place a
    protected location links to; a link must not be a way around the check in either
    direction.
    """
    return (
        protected_reason(requested) or protected_reason(resolved) or _alias_reason(resolved, roots)
    )


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: Windows junctions and symlinks


def _is_link_entry(entry: os.DirEntry[str]) -> bool:
    """A symbolic link **or a Windows junction** (``is_symlink`` misses junctions before 3.12)."""
    try:
        if entry.is_symlink():
            return True
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True  # cannot tell: do not descend
    return bool(attributes & _REPARSE_POINT)


def protected_descendant_reason(source: Path, destination: Path) -> str:
    """Would moving the folder *source* to *destination* install something protected there?

    Walks the source tree (no link is followed, the walk is bounded) and judges each entry
    by the path it would have under *destination*. A tree that is too big to inspect is
    reported rather than assumed harmless.
    """
    if not source.is_dir():
        return ""
    seen = 0
    stack: list[tuple[Path, Path, int]] = [(source, destination, 0)]
    while stack:
        folder, target, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_MOVE_SCAN_ENTRIES:
                        return (
                            "moves a folder too large to inspect for protected files "
                            f"(more than {MAX_MOVE_SCAN_ENTRIES} entries)"
                        )
                    moved_to = target / entry.name
                    reason = protected_reason(moved_to)
                    if reason:
                        return reason
                    try:
                        descend = entry.is_dir(follow_symlinks=False) and not _is_link_entry(entry)
                    except OSError:
                        descend = False
                    if descend:
                        if depth + 1 > MAX_MOVE_SCAN_DEPTH:
                            return "moves a folder nested too deeply to inspect for protected files"
                        stack.append((Path(entry.path), moved_to, depth + 1))
        except OSError:
            return "moves a folder that could not be fully inspected for protected files"
    return ""


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

    def lexical(self, raw: str | Path) -> Path:
        """The path as requested: anchored to the workspace, ``..`` collapsed, links not followed."""
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        return Path(os.path.abspath(candidate))

    def protected(self, raw: str | Path, resolved: Path | None = None) -> str:
        """Why changing *raw* is high-risk (see :func:`protected_for`), or ""."""
        resolved = resolved if resolved is not None else self.resolve(raw)
        roots = [self.workspace_root, *sorted(self.extra_roots)]
        return protected_for(self.lexical(raw), resolved, roots)

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
    from velune.permissions.actions import MUTATING_TYPES, Action, Risk

    resolved = boundary.resolve(raw)
    secret = is_secret_path(resolved)
    outside = not boundary.inside(resolved)
    if risk is None:
        risk = Risk.HIGH if secret else Risk.LOW
    protected = boundary.protected(raw, resolved) if action_type in MUTATING_TYPES else ""
    if protected:
        risk = Risk.HIGH
        reason = f"{reason}: {protected}" if reason else protected
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
