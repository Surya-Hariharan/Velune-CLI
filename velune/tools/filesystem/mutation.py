"""Safe file mutation shared by every tool that changes files.

``write_file``, ``edit_file``, ``create_file``, ``delete_file``, the folder and
move tools, and the Council's edit applier all change files through this module,
so they share one set of guarantees:

* **Atomic replacement.** New content is written to a temporary file in the
  target's own folder, flushed and ``fsync``-ed, then swapped in with
  :func:`os.replace`. A crash or error leaves either the old file or the new one,
  never a half-written file, and the temporary file is removed. The folder
  entry itself is not ``fsync``-ed, so after a power loss the rename may not have
  reached disk yet; that is the guarantee, and no stronger one is claimed.
* **Encoding and line-ending preservation.** An existing file keeps its encoding,
  byte-order mark and line-ending style (LF, CRLF, or the dominant style of a mixed
  file). Text from the model is LF; it is converted to the file's style before
  writing, so changing one line never rewrites every line ending.
* **Conflict detection.** :class:`FileVersionTracker` remembers the hash of each
  file as the agent last saw it (by reading or writing it). If the file has changed
  on disk since, the write is refused with :class:`FileConflictError` and the agent
  is told to read it again. The hash is checked again immediately before the swap.
* **Verification.** After a write the file is hashed again and must match the bytes
  that were written; moves, deletes and folder changes are checked with ``stat``.
  A failed check raises :class:`MutationVerificationError`, so the agent is never
  told that a change succeeded when it did not.
* **Undo.** Each change is recorded in the active :class:`TurnJournal` (one per agent
  turn), so ``/rewind`` can put the files back.

Paths reaching this module have already been resolved and authorized by the
permission gate (:mod:`velune.permissions.gate`); :func:`atomic_write` still
re-checks that the target is inside the active boundary right before the swap.
"""

from __future__ import annotations

import codecs
import contextvars
import difflib
import hashlib
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Largest file ``edit_file`` / ``write_file`` will load to modify in place.
MAX_MUTATION_BYTES = 20 * 1024 * 1024
#: Largest amount of data the undo journal keeps for one change (a file's previous
#: content, or a deleted folder's tree). Beyond it the change cannot be rewound.
JOURNAL_MAX_BYTES = 20 * 1024 * 1024
#: Characters of unified diff returned to the model per change.
DIFF_MAX_CHARS = 6000
#: Marker for "the file must not exist" in hash comparisons.
ABSENT = "absent"

_TMP_PREFIX = ".velune-tmp-"
_HASH_CHUNK = 1024 * 1024


class FileConflictError(RuntimeError):
    """The file changed on disk since the agent last saw it."""


class MutationVerificationError(RuntimeError):
    """A change was made but the result on disk is not what was intended."""


# ── Hashing ────────────────────────────────────────────────────────────────────


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    """Hash of the file's bytes, or :data:`ABSENT` if it is missing (or not a file)."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, PermissionError):
        if path.is_dir() or not path.exists():
            return ABSENT
        raise


# ── Text decoding with preserved encoding and line endings ────────────────────


@dataclass(frozen=True)
class TextFile:
    """A text file as loaded for modification. ``text`` keeps its original line endings."""

    path: Path
    raw: bytes
    text: str
    encoding: str
    bom: bytes
    newline: str  # dominant line ending: "\n" or "\r\n"
    style: str  # "lf" | "crlf" | "mixed" | "none"
    sha256: str


def newline_style(text: str) -> tuple[str, str]:
    """``(style, dominant newline)`` for *text*; files without newlines default to LF."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed", ("\r\n" if crlf >= lf else "\n")
    if crlf:
        return "crlf", "\r\n"
    if lf:
        return "lf", "\n"
    return "none", "\n"


def to_newline(text: str, newline: str) -> str:
    """Convert *text*'s line endings (LF or CRLF) to *newline*."""
    text = text.replace("\r\n", "\n")
    return text if newline == "\n" else text.replace("\n", newline)


def _split_bom(data: bytes) -> tuple[bytes, str | None]:
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            return bom, encoding
    return b"", None


def decode_text(data: bytes, name: str) -> tuple[str, str, bytes]:
    """``(text, encoding, bom)`` for *data*; raises ``ValueError`` for binary or undecodable data.

    The BOM is split off and kept, so the file is written back with the same one.
    """
    from velune.tools.filesystem.read import NotTextError, detect_text_encoding

    if not data:
        return "", "utf-8", b""
    bom, encoding = _split_bom(data)
    body = data[len(bom) :]
    if encoding is None:
        try:
            encoding = detect_text_encoding(data, name, len(data), whole_file=True)
        except NotTextError as exc:
            raise ValueError(
                f"{name} is a binary file; it cannot be edited as text. "
                "Use write_file to replace it entirely, or leave it unchanged."
            ) from exc
    try:
        return body.decode(encoding), encoding, bom
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name}: could not decode as {encoding}; the file is corrupt.") from exc


def encode_text(text: str, encoding: str, bom: bytes = b"") -> bytes:
    try:
        return bom + text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"The new content contains characters that cannot be saved in this file's "
            f"encoding ({encoding}): {exc.object[exc.start : exc.end]!r}. Keep to characters "
            "the file can store, or rewrite the whole file as UTF-8 with write_file."
        ) from exc


def load_text(path: Path, display: str | None = None) -> TextFile:
    """Load *path* for modification, keeping its encoding, BOM and line endings."""
    name = display or str(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {name}")
    if path.is_dir():
        raise IsADirectoryError(f"{name} is a folder, not a file")
    size = path.stat().st_size
    if size > MAX_MUTATION_BYTES:
        raise ValueError(
            f"{name} is {size} bytes, over the {MAX_MUTATION_BYTES}-byte limit for editing."
        )
    raw = path.read_bytes()
    text, encoding, bom = decode_text(raw, name)
    style, newline = newline_style(text)
    return TextFile(
        path=path,
        raw=raw,
        text=text,
        encoding=encoding,
        bom=bom,
        newline=newline,
        style=style,
        sha256=sha256_bytes(raw),
    )


# ── Atomic writes ──────────────────────────────────────────────────────────────


def _check_boundary(path: Path) -> None:
    """Refuse a target that left the authorized boundary since the gate checked it.

    Resolving the parent here catches a folder swapped for a symlink to somewhere
    else between authorization and the write.
    """
    from velune.execution.path_guard import PathTraversalError
    from velune.permissions.gate import active_boundary

    boundary = active_boundary()
    if boundary is None:
        return
    resolved = path.parent.resolve() / path.name
    if not boundary.inside(resolved):
        raise PathTraversalError(f"'{path}' now resolves outside the workspace ('{resolved}').")


#: Names Windows reserves for devices. A file called ``CON`` or ``nul.txt`` can be created
#: through some APIs but then cannot be opened, renamed or deleted by ordinary tools.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def _refuse_unsafe_target(path: Path) -> None:
    """Refuse a target that cannot be written safely, before anything is created."""
    if os.name == "nt" and path.name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES:
        raise ValueError(
            f"'{path.name}' is a reserved Windows device name; a file with that name cannot "
            "be managed afterwards. Choose another file name."
        )
    # A file the user marked read-only stays as it is. Without this, Windows refuses the
    # swap after a delay and POSIX would silently replace it (rename ignores the file's
    # own mode), which defeats the protection.
    if path.is_file() and not os.access(path, os.W_OK):
        raise PermissionError(
            f"{path.name} is read-only, so it was not modified. Make it writable first if "
            "the change is intended."
        )


def _discard(tmp: str) -> None:
    """Remove a temporary file, even one that inherited a read-only attribute."""
    for _ in range(2):
        try:
            os.unlink(tmp)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            try:
                os.chmod(tmp, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                return
        except OSError:
            return


def _replace(source: str, target: Path) -> None:
    # On Windows a file held open by another program (an editor, an indexer, an
    # antivirus scan) refuses replacement for a moment; retry briefly.
    attempts = 6 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write(path: Path, data: bytes, *, expected_sha256: str | None = None) -> str:
    """Replace *path* with *data* atomically and return the new content's hash.

    *expected_sha256* (a hash, or :data:`ABSENT` for "must not exist yet") is
    compared with the file on disk immediately before the swap; a mismatch raises
    :class:`FileConflictError` and leaves the file untouched. The file's permission
    bits are kept. The temporary file is removed if anything fails.
    """
    _check_boundary(path)
    _refuse_unsafe_target(path)
    ensure_parent(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=_TMP_PREFIX, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_file():
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass  # permissions are best effort; the content is what matters
        if expected_sha256 is not None:
            current = file_sha256(path)
            if current != expected_sha256:
                raise FileConflictError(conflict_message(path, expected_sha256, current))
        _check_boundary(path)
        _replace(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return sha256_bytes(data)


def ensure_parent(path: Path) -> None:
    """Create *path*'s missing parent folders, journaling each so ``/rewind`` removes them."""
    missing: list[Path] = []
    probe = path.parent
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    if not missing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    for folder in reversed(missing):  # outermost first; rewind runs newest first
        journal(DirCreated(folder))


def verify_written(path: Path, expected_sha256: str) -> None:
    actual = file_sha256(path)
    if actual != expected_sha256:
        raise MutationVerificationError(
            f"Wrote {path}, but its content on disk does not match what was written "
            f"(expected sha256 {expected_sha256[:12]}, found {actual[:12]}). "
            "Another program may have changed it; read it again before continuing."
        )


def conflict_message(path: Path, expected: str, current: str) -> str:
    found = "it no longer exists" if current == ABSENT else f"sha256 is now {current[:12]}"
    seen = "did not exist" if expected == ABSENT else f"had sha256 {expected[:12]}"
    return (
        f"Conflict: {path} changed on disk since you last read or wrote it (it {seen}; "
        f"{found}). It was not modified. Read it again with read_file, then retry the change "
        "against its current content."
    )


# ── Conflict tracking ──────────────────────────────────────────────────────────


class FileVersionTracker:
    """The hash of each file as the agent last saw it, for conflict detection.

    One tracker is shared by the read, write and edit tools of a session (see
    :mod:`velune.tools.subsystems`). A file the agent never read or wrote is not
    tracked, so changing it is not a conflict.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    def record(self, path: Path, sha: str) -> None:
        self._seen[self._key(path)] = sha

    def forget(self, path: Path) -> None:
        self._seen.pop(self._key(path), None)

    def seen(self, path: Path) -> str | None:
        return self._seen.get(self._key(path))

    def check(self, path: Path, current_sha: str) -> None:
        """Raise :class:`FileConflictError` if *path* changed since it was last seen."""
        seen = self.seen(path)
        if seen is not None and seen != current_sha:
            raise FileConflictError(conflict_message(path, seen, current_sha))


# ── Diffs and results ──────────────────────────────────────────────────────────


def unified_diff(old: str, new: str, label: str) -> tuple[str, int, int]:
    """``(diff text, lines added, lines removed)``; the text is capped at :data:`DIFF_MAX_CHARS`."""
    old_lines = old.replace("\r\n", "\n").splitlines()
    new_lines = new.replace("\r\n", "\n").splitlines()
    lines = list(
        difflib.unified_diff(old_lines, new_lines, f"a/{label}", f"b/{label}", lineterm="", n=2)
    )
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    text = "\n".join(lines)
    if len(text) > DIFF_MAX_CHARS:
        text = text[:DIFF_MAX_CHARS] + f"\n… [diff truncated: {len(text)} characters in total]"
    return text, added, removed


@dataclass
class MutationResult:
    """What a filesystem tool reports back to the model.

    The first line is stable and parseable: ``<op> OK <path> (+A -R) verified sha256=<hash>``.
    """

    op: str
    path: str
    added: int | None = None
    removed: int | None = None
    diff: str = ""
    verified: bool = False
    sha256: str | None = None
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"{self.op} OK {self.path}"
        if self.added is not None and self.removed is not None:
            head += f" (+{self.added} -{self.removed})"
        if self.verified:
            head += " verified"
        if self.sha256 and self.sha256 != ABSENT:
            head += f" sha256={self.sha256}"
        parts = [head, *self.notes]
        if self.diff:
            parts.append(self.diff)
        return "\n".join(parts)


def display_path(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(Path(workspace).resolve()).as_posix()
    except ValueError:
        return str(path)


# ── Undo journal (/rewind) ─────────────────────────────────────────────────────


@dataclass
class FileChange:
    path: Path
    before: bytes | None  # None: the file did not exist
    after_sha: str  # ABSENT: the change deleted it


@dataclass
class DirCreated:
    path: Path


@dataclass
class DirDeleted:
    path: Path
    # (relative path, bytes) for files, (relative path, None) for folders; None: too
    # large to keep, so this deletion cannot be rewound.
    snapshot: list[tuple[str, bytes | None]] | None


@dataclass
class Moved:
    source: Path
    destination: Path
    overwritten: bytes | None  # a destination file the move replaced
    after_sha: str | None  # the moved file's hash (None for a folder)


@dataclass
class RewindReport:
    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def is_link(path: Path) -> bool:
    """True for a symbolic link **or a Windows junction / other reparse point**.

    ``Path.is_symlink()`` is False for a directory junction before Python 3.12, so
    code that relied on it would walk *through* a junction into whatever it points at,
    which may lie outside the workspace.
    """
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            attributes = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)  # type: ignore[attr-defined]
    return False


def count_entries(root: Path) -> int:
    """Number of files and folders under *root*, counting a link but never walking into one."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        total += len(filenames) + len(dirnames)
        dirnames[:] = [name for name in dirnames if not is_link(base / name)]
    return total


def snapshot_tree(
    root: Path, limit: int = JOURNAL_MAX_BYTES
) -> list[tuple[str, bytes | None]] | None:
    """Every folder and file under *root*, or None if it holds links or exceeds *limit* bytes."""
    entries: list[tuple[str, bytes | None]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames:
            child = base / name
            if is_link(child):
                return None
            entries.append((child.relative_to(root).as_posix(), None))
        for name in filenames:
            child = base / name
            if is_link(child):
                return None
            total += child.stat().st_size
            if total > limit:
                return None
            entries.append((child.relative_to(root).as_posix(), child.read_bytes()))
    return entries


def read_before_image(path: Path) -> tuple[bytes | None, bool]:
    """``(previous bytes or None if absent, whether it fits in the journal)``."""
    if not path.is_file():
        return None, True
    if path.stat().st_size > JOURNAL_MAX_BYTES:
        return None, False
    return path.read_bytes(), True


class TurnJournal:
    """The file changes made during one agent turn, in order, for ``/rewind``."""

    def __init__(self) -> None:
        self.entries: list[FileChange | DirCreated | DirDeleted | Moved] = []
        self.irreversible: list[str] = []

    def __bool__(self) -> bool:
        return bool(self.entries or self.irreversible)

    def add(self, entry: FileChange | DirCreated | DirDeleted | Moved) -> None:
        self.entries.append(entry)

    def mark_irreversible(self, description: str) -> None:
        self.irreversible.append(description)

    def describe(self) -> list[str]:
        out: list[str] = []
        for e in self.entries:
            if isinstance(e, FileChange):
                verb = (
                    "created"
                    if e.before is None
                    else ("deleted" if e.after_sha == ABSENT else "modified")
                )
                out.append(f"{verb} {e.path}")
            elif isinstance(e, DirCreated):
                out.append(f"created folder {e.path}")
            elif isinstance(e, DirDeleted):
                out.append(f"deleted folder {e.path}")
            else:
                out.append(f"moved {e.source} → {e.destination}")
        return out

    def rewind(self) -> RewindReport:
        """Undo the turn's changes, newest first.

        A file that changed again since the turn (by the user or another program) is
        skipped and reported rather than overwritten.
        """
        report = RewindReport()
        for entry in reversed(self.entries):
            try:
                self._undo(entry, report)
            except OSError as exc:
                report.skipped.append(f"{_entry_path(entry)}: {exc}")
        for description in self.irreversible:
            report.skipped.append(f"{description}: too large to keep, cannot be rewound")
        self.entries.clear()
        self.irreversible.clear()
        return report

    @staticmethod
    def _undo(entry: FileChange | DirCreated | DirDeleted | Moved, report: RewindReport) -> None:
        if isinstance(entry, FileChange):
            if file_sha256(entry.path) != entry.after_sha:
                report.skipped.append(f"{entry.path}: changed since this turn, left as it is")
                return
            if entry.before is None:
                entry.path.unlink()
            else:
                atomic_write(entry.path, entry.before, expected_sha256=entry.after_sha)
            report.restored.append(str(entry.path))
        elif isinstance(entry, DirCreated):
            if entry.path.is_dir() and not any(entry.path.iterdir()):
                entry.path.rmdir()
                report.restored.append(str(entry.path))
            else:
                report.skipped.append(f"{entry.path}: folder is not empty, left as it is")
        elif isinstance(entry, DirDeleted):
            if entry.snapshot is None:
                report.skipped.append(f"{entry.path}: was too large to keep, cannot be restored")
                return
            if entry.path.exists():
                report.skipped.append(f"{entry.path}: something exists there now, left as it is")
                return
            entry.path.mkdir(parents=True)
            for rel, data in entry.snapshot:
                target = entry.path / rel
                if data is None:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            report.restored.append(str(entry.path))
        else:
            dst, src = entry.destination, entry.source
            unchanged = entry.after_sha is None or file_sha256(dst) == entry.after_sha
            if not dst.exists() or src.exists() or not unchanged:
                report.skipped.append(f"{dst}: changed since this turn, left as it is")
                return
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            if entry.overwritten is not None:
                atomic_write(dst, entry.overwritten, expected_sha256=ABSENT)
            report.restored.append(f"{dst} → {src}")


def _entry_path(entry: FileChange | DirCreated | DirDeleted | Moved) -> Path:
    return entry.destination if isinstance(entry, Moved) else entry.path


_ACTIVE_JOURNAL: contextvars.ContextVar[TurnJournal | None] = contextvars.ContextVar(
    "velune_active_journal", default=None
)


def active_journal() -> TurnJournal | None:
    return _ACTIVE_JOURNAL.get()


def set_active_journal(journal: TurnJournal | None) -> contextvars.Token:
    return _ACTIVE_JOURNAL.set(journal)


def reset_active_journal(token: contextvars.Token) -> None:
    _ACTIVE_JOURNAL.reset(token)


def journal(entry: FileChange | DirCreated | DirDeleted | Moved) -> None:
    """Record *entry* in the active turn's journal, if a turn is recording."""
    current = _ACTIVE_JOURNAL.get()
    if current is not None:
        current.add(entry)


def journal_irreversible(description: str) -> None:
    current = _ACTIVE_JOURNAL.get()
    if current is not None:
        current.mark_irreversible(description)
