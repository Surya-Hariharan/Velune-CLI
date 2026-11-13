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
import uuid
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


# The three reasons a file cannot be handled as editable text. Each is a ``ValueError`` so
# callers that only distinguish "not editable text" keep working; callers that must treat
# them differently (``write_file`` replaces a binary file but never an oversized text file)
# catch the specific one.


class FileTooLargeError(ValueError):
    """The file is over a size limit, so its content was not read into memory."""

    def __init__(self, message: str, *, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        self.limit = limit


class BinaryFileError(ValueError):
    """The file holds binary data, not text."""


class TextEncodingError(ValueError):
    """The file looks like text, but not in an encoding it can be safely decoded and kept in."""


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


def read_bounded(path: Path, limit: int, display: str | None = None) -> bytes:
    """The bytes of *path*, reading at most ``limit + 1`` of them.

    Raises :class:`FileTooLargeError` if the file holds more than *limit* bytes. A file
    whose size is already over the limit is refused before any of it is read; the size is
    also checked while reading, so a file that grows after it was measured still cannot
    make this read more than the limit.
    """
    chunks: list[bytes] = []
    total = 0
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size <= limit:
            while total <= limit:
                chunk = handle.read(min(_HASH_CHUNK, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
    if size > limit or total > limit:
        name = display or str(path)
        raise FileTooLargeError(
            f"{name} is larger than the {limit}-byte limit, so it was not read.",
            size=max(size, total),
            limit=limit,
        )
    return b"".join(chunks)


def _read_prefix(path: Path, limit: int) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(limit)


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
    """``(text, encoding, bom)`` for *data*.

    Raises :class:`BinaryFileError` for binary data and :class:`TextEncodingError` for text
    that cannot be decoded. The BOM is split off and kept, so the file is written back with
    the same one.
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
            raise BinaryFileError(
                f"{name} is a binary file; it cannot be edited as text. "
                "Use write_file to replace it entirely, or leave it unchanged."
            ) from exc
        except ValueError as exc:
            raise TextEncodingError(str(exc)) from exc
    try:
        return body.decode(encoding), encoding, bom
    except UnicodeDecodeError as exc:
        raise TextEncodingError(
            f"{name}: could not decode as {encoding}; the file is corrupt."
        ) from exc


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
        raise FileTooLargeError(
            f"{name} is {size} bytes, over the {MAX_MUTATION_BYTES}-byte limit for editing.",
            size=size,
            limit=MAX_MUTATION_BYTES,
        )
    raw = read_bounded(path, MAX_MUTATION_BYTES, name)
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


# ── Replacing a whole file ────────────────────────────────────────────────────

#: Bytes read from the start of a file too large to load, to tell text from binary and to
#: find its byte-order mark and line-ending style.
_SAMPLE_BYTES = 64 * 1024


@dataclass(frozen=True)
class Overwrite:
    """How to replace a file's whole content, decided before anything is written."""

    data: bytes  # the new bytes to write
    expected_sha256: str  # hash of the file as it is now (ABSENT: it does not exist)
    exists: bool
    #: The previous text, for the diff; None when the file is too large to show.
    old_text: str | None
    #: The previous bytes, kept so /rewind can put them back (see ``kept``).
    before: bytes | None
    #: False for an existing file whose previous bytes are too large for the journal:
    #: the change is recorded as irreversible, never as a creation.
    kept: bool
    note: str | None = None


def _journal_copy(path: Path, raw: bytes | None = None) -> tuple[bytes | None, bool, str]:
    """``(previous bytes or None, kept, hash)`` for an existing file about to be replaced.

    The journal limit is applied on its own, separately from the editing limit. At most
    :data:`JOURNAL_MAX_BYTES` (+1) bytes are read; a larger file is hashed in chunks.
    """
    if raw is not None:
        sha = sha256_bytes(raw)
        return (raw, True, sha) if len(raw) <= JOURNAL_MAX_BYTES else (None, False, sha)
    if path.stat().st_size <= JOURNAL_MAX_BYTES:
        try:
            data = read_bounded(path, JOURNAL_MAX_BYTES)
            return data, True, sha256_bytes(data)
        except FileTooLargeError:
            pass  # it grew since it was measured
    return None, False, file_sha256(path)


def plan_overwrite(path: Path, content: str, display: str | None = None) -> Overwrite:
    """Plan replacing *path*'s whole content with *content*, reading a bounded amount of it.

    * A new file is written as UTF-8 exactly as given.
    * A text file up to :data:`MAX_MUTATION_BYTES` keeps its encoding, BOM and dominant
      line ending.
    * A binary file is replaced by the UTF-8 text, as before.
    * A larger file is never read whole. With a byte-order mark its encoding is known
      for certain, so the BOM, encoding and the line ending seen at the start are kept.
      Without one, a binary file is replaced as above, but a text file is refused
      (:class:`FileTooLargeError`): its encoding cannot be confirmed without reading it all,
      and converting it silently is not acceptable.
    * Text that cannot be decoded is refused (:class:`TextEncodingError`) rather than
      overwritten in another encoding.

    The previous bytes are kept for /rewind only up to :data:`JOURNAL_MAX_BYTES`; beyond
    that ``kept`` is False and the caller records the change as irreversible.
    """
    name = display or str(path)
    if path.is_dir():
        raise IsADirectoryError(f"{name} is a folder, not a file")
    if not path.exists():
        return Overwrite(content.encode("utf-8"), ABSENT, False, "", None, True)

    raw: bytes | None = None
    if path.stat().st_size <= MAX_MUTATION_BYTES:
        try:
            raw = read_bounded(path, MAX_MUTATION_BYTES, name)
        except FileTooLargeError:
            raw = None  # it grew past the limit since it was measured
    if raw is not None:
        try:
            old_text, encoding, bom = decode_text(raw, name)
        except BinaryFileError:
            before, kept, sha = _journal_copy(path, raw)
            return Overwrite(content.encode("utf-8"), sha, True, "", before, kept)
        style, newline = newline_style(old_text)
        text = to_newline(content, newline) if style != "none" else content
        before, kept, sha = _journal_copy(path, raw)
        return Overwrite(encode_text(text, encoding, bom), sha, True, old_text, before, kept)
    return _plan_oversized(path, content, name)


def _plan_oversized(path: Path, content: str, name: str) -> Overwrite:
    from velune.tools.filesystem.read import NotTextError, detect_text_encoding

    size = path.stat().st_size
    sample = _read_prefix(path, _SAMPLE_BYTES)
    bom, encoding = _split_bom(sample)
    if encoding is None:
        try:
            detect_text_encoding(sample, name, size, whole_file=False)
        except NotTextError:
            before, kept, sha = _journal_copy(path)
            return Overwrite(content.encode("utf-8"), sha, True, None, before, kept)
        except ValueError as exc:
            raise TextEncodingError(f"{exc} It was not replaced.") from exc
        raise FileTooLargeError(
            f"{name} is {size} bytes, over the {MAX_MUTATION_BYTES}-byte limit for rewriting a "
            "text file. Its encoding cannot be confirmed without reading all of it, so it was "
            "not replaced. Delete it first (delete_file) if replacing it entirely is intended.",
            size=size,
            limit=MAX_MUTATION_BYTES,
        )
    # A partial sample may end inside a character; only the bytes before that are decoded.
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        head = decoder.decode(sample[len(bom) :], final=False)
    except UnicodeDecodeError as exc:
        raise TextEncodingError(
            f"{name}: could not decode as {encoding}; the file is corrupt. It was not replaced."
        ) from exc
    style, newline = newline_style(head)
    text = to_newline(content, newline) if style != "none" else content
    before, kept, sha = _journal_copy(path)
    return Overwrite(
        encode_text(text, encoding, bom),
        sha,
        True,
        None,
        before,
        kept,
        note=f"(previous content: {size} bytes, too large to show a diff; kept its {encoding} "
        "encoding and byte-order mark)",
    )


def journal_overwrite(path: Path, plan: Overwrite, new_sha256: str) -> None:
    """Record a replacement planned by :func:`plan_overwrite` in the active journal."""
    if not plan.exists:
        journal(FileChange(path, None, new_sha256))
    elif plan.kept:
        journal(FileChange(path, plan.before, new_sha256))
    else:
        # Journaling it as a creation (before=None) would make /rewind delete the file and
        # call it restored; say plainly that it cannot be rewound instead.
        journal_irreversible(f"overwrote {path}")


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


#: On POSIX a new file gets the mode a plain ``open(path, "w")`` would give it: 0o666 with the
#: process umask applied by the kernel. (``mkstemp`` would force 0o600.) Elsewhere — Windows,
#: where permission bits are not the access model — the temporary file is left as created.
_NEW_FILE_FOLLOWS_UMASK = os.name == "posix"
_NEW_FILE_MODE = 0o666
_PRIVATE_MODE = 0o600  # what a temporary file that will be given an explicit mode starts as


def _create_temp(
    directory: Path, *, follow_umask: bool, create_mode: int = _NEW_FILE_MODE
) -> tuple[int, str]:
    """Create a uniquely named temporary file next to the target; ``(fd, path)``.

    With *follow_umask* the file is created with *create_mode* (0o666 by default) and the
    **kernel** applies the process umask atomically, exactly as for any ordinary new file. The
    umask is never read or changed here (``os.umask`` can only be read by setting it, which
    would race with other threads creating files), and no mode such as 0o644 is assumed. The
    file is never more permissive than *create_mode* and the umask allow, and it is exclusive
    (``O_EXCL``) and not followed through a link (``O_NOFOLLOW``), like ``mkstemp``'s.
    """
    if not follow_umask:
        fd, name = tempfile.mkstemp(dir=directory, prefix=_TMP_PREFIX, suffix=".part")
        return fd, name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    for _ in range(100):
        name = str(directory / f"{_TMP_PREFIX}{uuid.uuid4().hex[:12]}.part")
        try:
            return os.open(name, flags, create_mode), name
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create a unique temporary file in {directory}")


def file_mode(path: Path) -> int | None:
    """The permission bits of *path* to restore later (POSIX; ``None`` elsewhere or on error).

    Setuid/setgid/sticky bits are left out: restoring a file must never add privilege.
    """
    if not _NEW_FILE_FOLLOWS_UMASK:
        return None
    try:
        return stat.S_IMODE(path.stat().st_mode) & 0o777
    except OSError:
        return None


def atomic_write(
    path: Path, data: bytes, *, expected_sha256: str | None = None, mode: int | None = None
) -> str:
    """Replace *path* with *data* atomically and return the new content's hash.

    *expected_sha256* (a hash, or :data:`ABSENT` for "must not exist yet") is
    compared with the file on disk immediately before the swap; a mismatch raises
    :class:`FileConflictError` and leaves the file untouched. Replacing an existing file
    keeps its permission bits; a new file gets the default a plain write would give it
    (on POSIX 0o666 reduced by the process umask), or *mode* when one is given (used to give
    a deleted file back the permissions it had). In that case the temporary file is created
    owner-only (0o600) and only widened to *mode* once the content is in it, so no other user
    can open it while its data is being written (a descriptor opened early would stay
    readable after a later ``chmod``). The temporary file is removed if anything fails.
    """
    _check_boundary(path)
    _refuse_unsafe_target(path)
    ensure_parent(path)
    replacing = path.is_file()
    explicit_mode = mode is not None and not replacing and _NEW_FILE_FOLLOWS_UMASK
    fd, tmp = _create_temp(
        path.parent,
        follow_umask=_NEW_FILE_FOLLOWS_UMASK and not replacing,
        create_mode=_PRIVATE_MODE if explicit_mode else _NEW_FILE_MODE,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if explicit_mode and mode is not None:
                os.fchmod(handle.fileno(), mode & 0o777)  # widen only after the data is in
        if replacing:
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
    #: Permission bits of a file the change deleted (POSIX), so rewinding gives it back with the
    #: mode it had (a private 0600 file must not come back umask-readable). None: not recorded.
    mode: int | None = None


@dataclass
class DirCreated:
    path: Path


@dataclass
class DirDeleted:
    path: Path
    # (relative path, bytes) for files, (relative path, None) for folders; None: too
    # large to keep, so this deletion cannot be rewound.
    snapshot: list[tuple[str, bytes | None]] | None
    #: Permission bits of each file in the snapshot, by relative path (POSIX; empty on
    #: Windows). None: the entry was recorded before modes were kept, and its files are
    #: restored with default permissions, as they always were.
    modes: dict[str, int] | None = None


@dataclass
class Moved:
    source: Path
    destination: Path
    overwritten: bytes | None  # a destination file the move replaced
    after_sha: str | None  # the moved file's hash (None for a folder)
    overwritten_mode: int | None = None  # permission bits of that replaced file (POSIX)


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


@dataclass(frozen=True)
class TreeSnapshot:
    """A folder's contents as kept for rewinding its deletion."""

    entries: list[tuple[str, bytes | None]]  # as in DirDeleted.snapshot
    modes: dict[str, int]  # permission bits of each file (POSIX only; empty elsewhere)


def capture_tree(root: Path, limit: int | None = None) -> TreeSnapshot | None:
    """Every folder and file under *root* with each file's permission bits.

    None if the tree holds links or more than *limit* bytes. No read goes past the bytes
    left in the budget, even if a file grows while the tree is walked. Setuid, setgid and
    sticky bits are left out of the modes: restoring a file must never add privilege.
    """
    if limit is None:
        limit = JOURNAL_MAX_BYTES  # read at call time, so the configured limit applies
    entries: list[tuple[str, bytes | None]] = []
    modes: dict[str, int] = {}
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
            info = child.stat()
            if total + info.st_size > limit:
                return None
            try:
                data = read_bounded(child, limit - total)
            except FileTooLargeError:
                return None
            total += len(data)
            rel = child.relative_to(root).as_posix()
            entries.append((rel, data))
            if _NEW_FILE_FOLLOWS_UMASK:
                modes[rel] = stat.S_IMODE(info.st_mode) & 0o777
    return TreeSnapshot(entries, modes)


def snapshot_tree(root: Path, limit: int | None = None) -> list[tuple[str, bytes | None]] | None:
    """Every folder and file under *root*, or None if it holds links or exceeds *limit* bytes."""
    snapshot = capture_tree(root, limit)
    return None if snapshot is None else snapshot.entries


def read_before_image(path: Path) -> tuple[bytes | None, bool]:
    """``(previous bytes or None if absent, whether it fits in the journal)``.

    Reads at most :data:`JOURNAL_MAX_BYTES` (+1) bytes, even if the file grows after it
    was measured.
    """
    if not path.is_file():
        return None, True
    if path.stat().st_size > JOURNAL_MAX_BYTES:
        return None, False
    try:
        return read_bounded(path, JOURNAL_MAX_BYTES), True
    except FileTooLargeError:
        return None, False


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
                atomic_write(
                    entry.path, entry.before, expected_sha256=entry.after_sha, mode=entry.mode
                )
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
            TurnJournal._restore_tree(entry, report)
        else:
            dst, src = entry.destination, entry.source
            unchanged = entry.after_sha is None or file_sha256(dst) == entry.after_sha
            if not dst.exists() or src.exists() or not unchanged:
                report.skipped.append(f"{dst}: changed since this turn, left as it is")
                return
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            if entry.overwritten is not None:
                atomic_write(
                    dst, entry.overwritten, expected_sha256=ABSENT, mode=entry.overwritten_mode
                )
            note = (
                " (folder moved back as a whole; its contents are not compared)"
                if entry.after_sha is None and src.is_dir()
                else ""
            )
            report.restored.append(f"{dst} → {src}{note}")

    @staticmethod
    def _restore_tree(entry: DirDeleted, report: RewindReport) -> None:
        """Recreate a deleted folder from its snapshot.

        Each file goes through :func:`atomic_write` with the mode it had: the temporary file
        is created owner-only and widened only once its content is written, so a private
        file is never readable by others, not even for a moment. A file that cannot be
        restored is named in the report and the folder is reported as partly restored,
        never as restored.
        """
        snapshot = entry.snapshot or []
        entry.path.mkdir(parents=True)
        files = [rel for rel, data in snapshot if data is not None]
        failed: list[str] = []
        for rel, data in snapshot:
            target = entry.path / rel
            try:
                if data is None:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                mode = None if entry.modes is None else entry.modes.get(rel)
                atomic_write(target, data, expected_sha256=ABSENT, mode=mode)
            except (OSError, ValueError, FileConflictError) as exc:
                failed.append(f"{rel} ({type(exc).__name__}: {exc})")
        if failed:
            shown = "; ".join(failed[:5]) + (
                f"; and {len(failed) - 5} more" if len(failed) > 5 else ""
            )
            report.skipped.append(
                f"{entry.path}: only partly restored, {len(failed)} of {len(files)} file(s) "
                f"could not be written back: {shown}"
            )
            return
        note = ""
        if entry.modes is None and files and _NEW_FILE_FOLLOWS_UMASK:
            note = " (its files' permissions were not recorded; they have default permissions)"
        report.restored.append(f"{entry.path}{note}")


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
