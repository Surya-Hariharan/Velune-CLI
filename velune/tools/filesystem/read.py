"""Filesystem read tools."""

import asyncio
import codecs
import io
import os
from pathlib import Path
from typing import Any, BinaryIO

from velune.permissions.gate import resolve_tool_path
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem.ignore import load_ignore

# ── read_file limits (defined here, in one place) ────────────────────────────
#: A whole-file read refuses files larger than this. A ranged read streams the file
#: and has no such limit, so a big file is still reachable a section at a time.
READ_MAX_FILE_BYTES = 5 * 1024 * 1024
#: Characters returned by one call. Kept under the tool loop's own 16 000-character
#: cut-off, so that cut never silently drops the end of a result.
READ_MAX_CHARS = 14_000
#: A ranged read gives up when ``start_line`` lies beyond this many characters of
#: scanning, rather than churning through a multi-gigabyte log.
READ_MAX_SCAN_CHARS = 64 * 1024 * 1024
#: Bytes inspected to decide a file is binary, and to choose its text encoding.
#: Whole-file reads up to this size run on the event loop; larger work goes to a thread.
_INLINE_MAX_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_SNIFF_BYTES = 8192
_ENCODING_SAMPLE_BYTES = 64 * 1024
_NUMBER_WIDTH = 6

#: Bytes that can appear in ordinary text (printable ASCII, high bytes, and the usual
#: whitespace/escape controls). A sample made mostly of anything else is binary.
_TEXT_BYTES = bytes({7, 8, 9, 10, 12, 13, 27} | (set(range(0x20, 0x100)) - {0x7F}))
_BINARY_RATIO = 0.30


class _NotText(Exception):
    """The file is readable but is not text; the message is the result to hand back."""


def _file_size(handle: BinaryIO) -> int:
    return os.fstat(handle.fileno()).st_size


def _read_bounded(handle: BinaryIO, limit: int) -> bytes:
    """Read up to *limit* bytes in chunks.

    ``handle.read(limit)`` would allocate *limit* bytes up front even for a tiny file.
    """
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = handle.read(min(_READ_CHUNK_BYTES, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _coerce_line(value: Any, name: str) -> int | None:
    """A 1-based line number from the model, which may send it as a number or a string."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value)
    else:
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if number < 1:
        raise ValueError(f"{name} must be 1 or greater (lines are numbered from 1), got {number}")
    return number


def _choose_encoding(sample: bytes, name: str, size: int, *, whole_file: bool) -> str:
    """The text encoding of *sample*, or raise ``_NotText`` for binary, ``ValueError`` if unreadable.

    A byte-order mark decides first (PowerShell and Notepad write UTF-16 and UTF-8 with
    one). Without one, NUL bytes or a mostly-control sample mean binary; otherwise UTF-8,
    then Windows-1252 as the fallback for legacy Windows text.
    """
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"

    window = sample[:_SNIFF_BYTES]
    if b"\x00" in window or (
        window and len(window.translate(None, _TEXT_BYTES)) / len(window) > _BINARY_RATIO
    ):
        raise _NotText(
            f"[{name} appears to be a binary file ({size} bytes); its contents are not shown.]"
        )

    # A multibyte character may be cut off at the end of a partial sample; that is not an error.
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(sample, final=whole_file)
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        sample.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{name}: could not decode as UTF-8 or Windows-1252; "
            "the file is in another encoding or is corrupt."
        ) from exc


def _row(number: int, text: str, length: int | None = None) -> str:
    """One numbered line, clipped if the single line alone exceeds the output budget.

    *length* is the line's true length when *text* is only its head.
    """
    row = f"{number:>{_NUMBER_WIDTH}}\t{text}"
    if len(row) <= READ_MAX_CHARS:
        return row
    keep = READ_MAX_CHARS - 120
    total = length if length is not None else len(text)
    return row[:keep] + f" … [line {number} clipped: {total} characters, showing {keep}]"


def _too_large(name: str, size: int, *, exact: bool) -> str:
    shown = f"{size} bytes" if exact else f"more than {size - 1} bytes"
    return (
        f"[{name} is {shown}, over the {READ_MAX_FILE_BYTES}-byte limit for reading a whole "
        "file. Read a section instead with start_line and end_line (1-based, inclusive), "
        "for example start_line=1, end_line=200.]"
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class ReadFile(BaseTool):
    """Tool for reading file contents."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    def get_name(self) -> str:
        return "read_file"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_description(self) -> str:
        return (
            "Read a file. With only file_path it returns the file as-is when it fits "
            f"(about {READ_MAX_CHARS} characters); otherwise, or when start_line/end_line are "
            "given, it returns numbered lines (1-based, inclusive) and a final line saying "
            "where to continue."
        )

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [path_action(ActionType.READ, args.get("file_path", ""), boundary, "read file")]

    async def execute(
        self,
        file_path: str,
        start_line: Any = None,
        end_line: Any = None,
    ) -> str:
        """Read file contents, or the 1-based inclusive line range ``start_line``..``end_line``."""
        start = _coerce_line(start_line, "start_line")
        end = _coerce_line(end_line, "end_line")
        if end is not None and start is None:
            start = 1
        if start is not None and end is not None and end < start:
            raise ValueError(f"end_line ({end}) must not be before start_line ({start})")

        path = resolve_tool_path(file_path, self.workspace, label="ReadFile")
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if path.is_dir():
            raise IsADirectoryError(f"'{file_path}' is a directory; use read_directory to list it")
        if not path.is_file():
            raise ValueError(f"'{file_path}' is not a regular file")

        try:
            if start is None and path.stat().st_size <= _INLINE_MAX_BYTES:
                # A small whole-file read costs less than a thread hand-off.
                return self._read(path, file_path, start, end)
            # Anything bigger, or a ranged scan, must not stall the event loop.
            return await asyncio.to_thread(self._read, path, file_path, start, end)
        except _NotText as notice:
            return str(notice)

    # ── reading ──────────────────────────────────────────────────────────────

    def _read(self, path: Path, name: str, start: int | None, end: int | None) -> str:
        # Open once and do everything on this handle: no window for the path to be swapped
        # between the checks and the read, and the handle is released on every exit.
        with open(path, "rb") as handle:
            size = _file_size(handle)
            if start is None:
                return self._read_whole(handle, name, size)
            return self._read_range(handle, name, size, start, end)

    def _read_whole(self, handle: BinaryIO, name: str, size: int) -> str:
        if size > READ_MAX_FILE_BYTES:
            return _too_large(name, size, exact=True)
        # The size above may be stale (a log still being written): the read enforces the limit.
        data = _read_bounded(handle, READ_MAX_FILE_BYTES + 1)
        if len(data) > READ_MAX_FILE_BYTES:
            return _too_large(name, len(data), exact=False)
        if not data:
            return ""

        encoding = _choose_encoding(data, name, len(data), whole_file=True)
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{name}: could not decode as {encoding}; the file is corrupt."
            ) from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")  # universal newlines, as before
        if len(text) <= READ_MAX_CHARS:
            return text

        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()
        out: list[str] = []
        used = 0
        for number, line in enumerate(lines, 1):
            row = _row(number, line)
            if out and used + len(row) + 1 > READ_MAX_CHARS:
                break
            out.append(row)
            used += len(row) + 1
        if len(out) < len(lines):
            out.append(
                f"[showing lines 1-{len(out)} of {len(lines)}; call read_file again with "
                f"start_line={len(out) + 1} to continue]"
            )
        return "\n".join(out)

    def _read_range(
        self, handle: BinaryIO, name: str, size: int, start: int, end: int | None
    ) -> str:
        sample = handle.read(_ENCODING_SAMPLE_BYTES)
        encoding = _choose_encoding(
            sample, name, size, whole_file=len(sample) < _ENCODING_SAMPLE_BYTES
        )
        handle.seek(0)
        text = io.TextIOWrapper(handle, encoding=encoding, errors="strict", newline=None)

        out: list[str] = []
        used = 0
        scanned = 0
        number = 0
        cut_at: int | None = None
        try:
            while True:
                chunk = text.readline(READ_MAX_CHARS + 1)  # bounded: never one unbounded line
                if not chunk:
                    break
                number += 1
                if not chunk.endswith("\n") and len(chunk) > READ_MAX_CHARS:
                    # A line longer than any result can hold: keep its head, skip the rest.
                    line = chunk
                    skipped = 0
                    while True:
                        rest = text.readline(65_536)
                        ended = rest.endswith("\n")
                        skipped += len(rest) - (1 if ended else 0)  # the newline is not content
                        if not rest or ended:
                            break
                    full_length = len(line) + skipped
                else:
                    line = chunk[:-1] if chunk.endswith("\n") else chunk
                    full_length = len(line)
                scanned += full_length + 1
                if number < start:
                    if scanned > READ_MAX_SCAN_CHARS:
                        raise ValueError(
                            f"{name}: start_line={start} is too far into this file to reach "
                            f"(scanning stops after {READ_MAX_SCAN_CHARS} characters). "
                            "Use grep_files to find the line you need within the first part of "
                            "the file, or read an earlier range."
                        )
                    continue
                if end is not None and number > end:
                    break
                row = _row(number, line, full_length)
                if out and used + len(row) + 1 > READ_MAX_CHARS:
                    cut_at = number
                    break
                out.append(row)
                used += len(row) + 1
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{name}: invalid {encoding} data near line {number}; "
                "the file mixes encodings or is corrupt."
            ) from exc
        finally:
            text.detach()  # the caller's ``with`` owns and closes the handle

        if not out:  # the loop only ends with nothing to show by reaching the end of the file
            return f"[{name} has {_plural(number, 'line')}; start_line={start} is past the end.]"
        if cut_at is not None:
            out.append(
                f"[showing lines {start}-{cut_at - 1}; call read_file again with "
                f"start_line={cut_at} to continue]"
            )
        return "\n".join(out)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to read",
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line to return (1-based). Use with end_line to read "
                    "a section of a large file.",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Last line to return (1-based, inclusive).",
                },
            },
            "required": ["file_path"],
        }


class ReadDirectory(BaseTool):
    """Tool for reading directory contents."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()
        self._ignore = load_ignore(self.workspace)

    def get_name(self) -> str:
        return "read_directory"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_description(self) -> str:
        return "List the contents of a directory"

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [
            path_action(ActionType.READ, args.get("directory_path", "."), boundary, "list folder")
        ]

    async def execute(self, directory_path: str) -> list[str]:
        """List directory contents, excluding .veluneignore patterns."""
        path = resolve_tool_path(directory_path, self.workspace, label="ReadDirectory")
        if not path.exists() or not path.is_dir():
            raise NotADirectoryError(f"Directory not found: {directory_path}")

        return [item.name for item in path.iterdir() if not self._ignore.is_ignored(item)]

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory_path": {
                    "type": "string",
                    "description": "Path to the directory to list",
                }
            },
            "required": ["directory_path"],
        }
