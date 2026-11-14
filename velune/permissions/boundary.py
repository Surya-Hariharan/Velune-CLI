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


def _folded(path: Path) -> str:
    return str(path).casefold()


def _alias_reason(resolved: Path, roots: list[Path]) -> str:
    """Is *resolved* a place a protected location really lives at, or a folder holding one?

    Catches a protected name that is a link to somewhere ordinary (``.velune`` -> ``cfg/live``
    makes ``cfg/live/hooks.json`` the live hook file), an ordinary name that links to a
    protected target, and a **folder that contains** such a target (moving, replacing or
    deleting ``cfg`` swaps the live hook file just as surely as writing it). Identity, not
    just text, decides the first two; the containing-folder test compares real paths.
    """
    folded = _folded(resolved)
    for root in roots:
        for location, whole_tree, reason in _canonical_protected(root):
            if _same_place(resolved, location):
                return reason + _LINK_NOTE
            if whole_tree and any(_same_place(parent, location) for parent in resolved.parents):
                return reason + _LINK_NOTE
            # An ancestor of the live location: its subtree is what the protection guards.
            if any(_folded(parent) == folded for parent in location.parents):
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


def protected_descendant_reason(
    source: Path, destination: Path, roots: list[Path] | tuple[Path, ...] = ()
) -> str:
    """Would moving the folder *source* to *destination* install something protected there?

    Walks the source tree (no link is followed, the walk is bounded) and judges each entry
    twice: by the name it would have under *destination*, and by the real place it would
    occupy (*destination* resolved through any links, below which the entry's relative path
    is placed) against the live protected locations of *roots*. The second catches a folder
    put where a protected link points, e.g. ``live/`` under a recreated ``cfg/`` when
    ``.velune`` links to ``cfg/live``. A tree too big to inspect is reported rather than
    assumed harmless.
    """
    if not source.is_dir():
        return ""
    locations = [item for root in roots for item in _canonical_protected(root)]
    real_destination = _safe_resolve(destination)
    seen = 0
    stack: list[tuple[Path, Path, Path, int]] = [(source, destination, real_destination, 0)]
    while stack:
        folder, target, real_target, depth = stack.pop()
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
                    real_moved_to = real_target / entry.name
                    reason = protected_reason(moved_to) or _landing_reason(real_moved_to, locations)
                    if reason:
                        return reason
                    try:
                        descend = entry.is_dir(follow_symlinks=False) and not _is_link_entry(entry)
                    except OSError:
                        descend = False
                    if descend:
                        if depth + 1 > MAX_MOVE_SCAN_DEPTH:
                            return "moves a folder nested too deeply to inspect for protected files"
                        stack.append((Path(entry.path), moved_to, real_moved_to, depth + 1))
        except OSError:
            return "moves a folder that could not be fully inspected for protected files"
    return ""


def _landing_reason(real_path: Path, locations: list[tuple[Path, bool, str]]) -> str:
    """Would something placed at *real_path* be a live protected location (or inside one)?

    Text comparison on real paths only: the path does not exist yet, so there is nothing to
    ``stat``, and the entries of a large tree must stay cheap to judge.
    """
    folded = _folded(real_path)
    for location, whole_tree, reason in locations:
        here = _folded(location)
        if folded == here or (whole_tree and folded.startswith(here + os.sep.casefold())):
            return reason + _LINK_NOTE
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

    def protected_roots(self) -> list[Path]:
        """The roots whose protected locations (``.git``, ``.velune``, ``velune.toml``) count."""
        return [self.workspace_root, *sorted(self.extra_roots)]

    def protected(self, raw: str | Path, resolved: Path | None = None) -> str:
        """Why changing *raw* is high-risk (see :func:`protected_for`), or ""."""
        resolved = resolved if resolved is not None else self.resolve(raw)
        return protected_for(self.lexical(raw), resolved, self.protected_roots())

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


def _git_object_path(token: str) -> str | None:
    """The path inside a git object name: ``REV:path``, ``:path`` or ``:N:path`` (None if none).

    ``git show HEAD:.env`` and ``git show :.env`` print a file's committed or staged contents
    and name it only inside that one argument, so the argument as a whole ("HEAD:.env") is not a
    secret file name. Deliberately lenient about what counts as one: any argument of a git
    command with a ``:`` is split, and only its path half is looked at, so an argument that is
    not an object name at all can at worst ask for an approval it did not need. A Windows drive
    path (a drive letter, as in C:/dir) is a path, not an object name, and is left to the usual check.
    """
    if ":" not in token:
        return None
    if len(token) >= 2 and token[0].isalpha() and token[1] == ":" and token[2:3] in ("", "\\", "/"):
        return None
    if token.startswith(":"):
        rest = token[1:]  # ":path" (index) or ":N:path" (stage N of a merge)
        if len(rest) >= 2 and rest[0] in "0123" and rest[1] == ":":
            rest = rest[2:]
        return rest or None
    path = token.split(":", 1)[1]
    return path or None


def command_paths_secret(command: str, boundary: Boundary, cwd: Path | None = None) -> list[Path]:
    """Credential/secret files (``.env``, keys, ``~/.ssh``) that a command line names.

    The command line is split the way the executor splits it, the first token (the
    executable) is skipped and ``--opt=value`` is checked by its value. Relative names are
    resolved against *cwd* (the folder the command will run in; the workspace by default), so
    a link in that folder that leads to a secret file is seen. Best effort, like
    :func:`command_paths_outside`: it catches what a command *names* (``cat .env``), not a
    recursive search that happens to read one (``grep -r KEY .``).
    """
    from velune.core.argv import split_command

    try:
        tokens = split_command(command)
    except ValueError:
        tokens = command.split()
    base = cwd if cwd is not None else boundary.workspace_root
    program = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if tokens else ""
    is_git = program in ("git", "git.exe")
    found: list[Path] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            if "=" not in token:
                continue
            token = token.split("=", 1)[1]
        if not token or "://" in token:
            continue
        if is_git:
            # git reads a committed or staged file through an object name (HEAD:.env), which
            # names the file without being a path to it; check the path half of it.
            blob = _git_object_path(token)
            if blob:
                # A path never contains ":" in a well-formed object name, so a stray one
                # (HEAD::.env) is ambiguous: look at each piece too, and ask rather than guess.
                hit = next(
                    (p for p in (blob, *blob.split(":")) if p and is_secret_path(Path(p))), None
                )
                if hit is not None:
                    found.append(base / hit)
                    continue
        try:
            candidate = Path(token).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            resolved = candidate.resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if is_secret_path(resolved) or is_secret_path(Path(token)):
            found.append(resolved)
    return found
