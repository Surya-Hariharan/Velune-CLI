"""Conflict protection for move_path (#34), case-only renames (#37) and ranged reads (#38).

#34: ``move_path(overwrite=true)`` replaced a destination file without consulting the
file-version tracker, so a file the user changed after the agent read it was overwritten
with no conflict, unlike ``write_file`` and ``edit_file``. The source was not checked either.

#37: on a case-insensitive filesystem (macOS by default, Windows) a case-only rename was
refused on Windows ("the same path": resolving rewrites the name to its stored spelling)
and, on macOS, could not be rewound: the old spelling "exists" after the rename because it
is the same file, so the undo was skipped with a false "changed since this turn".

#38: a ranged ``read_file`` hashed the file again by path after closing its handle, so a
change made in between was recorded as the version the agent had seen.

The case-insensitive tests run where the filesystem really is case-insensitive (the
Windows and macOS CI jobs); the case-sensitive ones where it is not (Linux). The rewind
decision is also driven on every platform with the same-entry check injected.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from velune.tools.filesystem import mutation as mut
from velune.tools.filesystem import read as read_mod
from velune.tools.filesystem.dirs import MovePath
from velune.tools.filesystem.edit import EditFile
from velune.tools.filesystem.read import ReadFile


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "case-probe.tmp"
    probe.write_text("x", newline="")
    try:
        return (folder / "CASE-PROBE.TMP").exists()
    finally:
        probe.unlink()


@pytest.fixture
def journal():
    entries = mut.TurnJournal()
    token = mut.set_active_journal(entries)
    try:
        yield entries
    finally:
        mut.reset_active_journal(token)


@pytest.fixture
def insensitive(tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem (Windows, macOS)")
    return tmp_path


@pytest.fixture
def sensitive(tmp_path):
    if _case_insensitive(tmp_path):
        pytest.skip("needs a case-sensitive filesystem (Linux)")
    return tmp_path


def _tools(ws: Path):
    tracker = mut.FileVersionTracker()
    return (
        tracker,
        ReadFile(workspace=ws, tracker=tracker),
        MovePath(workspace=ws, tracker=tracker),
        EditFile(workspace=ws, tracker=tracker),
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _listing(folder: Path) -> list[str]:
    return sorted(name for name in os.listdir(folder) if not name.startswith(mut._TMP_PREFIX))


def _no_temporaries(folder: Path) -> bool:
    return not [name for name in os.listdir(folder) if name.startswith(mut._TMP_PREFIX)]


class _NothingMoves:
    """Fails the test if move_path moves or replaces anything."""

    def __init__(self, monkeypatch) -> None:
        import velune.tools.filesystem.dirs as dirs_mod

        def refuse(*args, **kwargs):
            raise AssertionError(f"something was moved before the conflict was raised: {args}")

        monkeypatch.setattr(dirs_mod.os, "replace", refuse)
        monkeypatch.setattr(dirs_mod.shutil, "move", refuse)
        monkeypatch.setattr(mut, "respell", refuse, raising=False)  # absent before #37


# ── #38: a ranged read records the version it returned ───────────────────────


def _changed_when_closed(monkeypatch, path: Path, new: bytes) -> None:
    """Rewrite *path* the moment read_file closes its handle: the old re-hash window."""
    real_open = open

    class Handle:
        def __init__(self, handle):
            self._handle = handle

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            result = self._handle.__exit__(*exc)
            path.write_bytes(new)
            return result

    def spy(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return Handle(handle) if Path(file) == path and "b" in mode else handle

    monkeypatch.setattr(read_mod, "open", spy, raising=False)


async def test_a_change_after_a_ranged_read_is_still_a_conflict(tmp_path, monkeypatch):
    tracker, read, _, edit = _tools(tmp_path)
    path = tmp_path / "f.txt"
    seen = b"line one\nline two\n"
    path.write_bytes(seen)
    _changed_when_closed(monkeypatch, path, b"line one\nline TWO, written by someone else\n")

    out = await read.execute(file_path="f.txt", start_line=1, end_line=2)
    monkeypatch.undo()

    assert "line two" in out and "TWO" not in out
    assert tracker.seen(path) == _sha(seen)  # the version whose lines were returned
    with pytest.raises(mut.FileConflictError):
        await edit.execute(file_path="f.txt", old_string="line one", new_string="LINE ONE")
    assert path.read_bytes() == b"line one\nline TWO, written by someone else\n"


async def test_a_file_changed_while_a_ranged_read_runs_is_a_conflict(tmp_path, monkeypatch):
    """The start is rewritten in place after it was read: the returned lines are the old
    ones, so the recorded version must not match the file as it is now."""
    tracker, read, _, edit = _tools(tmp_path)
    path = tmp_path / "f.txt"
    original = b"".join(b"line %05d\n" % n for n in range(20_000))  # several read chunks
    path.write_bytes(original)
    real_readinto = read_mod._HashingReader.readinto
    state = {"calls": 0}

    def readinto(self, buffer):
        state["calls"] += 1
        if state["calls"] == 2:  # the first chunk, with lines 1-3, has been read and hashed
            with open(path, "r+b") as handle:
                handle.write(b"LINE 00000\n")  # same length, so the rest is unchanged
        return real_readinto(self, buffer)

    monkeypatch.setattr(read_mod._HashingReader, "readinto", readinto)
    out = await read.execute(file_path="f.txt", start_line=1, end_line=3)
    monkeypatch.undo()

    assert state["calls"] >= 2
    assert "line 00000" in out and "LINE" not in out
    assert tracker.seen(path) == _sha(original)  # the bytes the lines came from
    assert tracker.seen(path) != mut.file_sha256(path)
    with pytest.raises(mut.FileConflictError):
        await edit.execute(file_path="f.txt", old_string="line 00001\n", new_string="X\n")


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"one line, no newline",
        b"a\r\nb\r\nc\r\n",
        b"".join(b"row %d\n" % n for n in range(50_000)),  # > the 64 KiB read chunks
        "ñandú\nüber\n".encode("utf-16"),
    ],
    ids=["empty", "single-line", "crlf", "multi-chunk", "utf-16"],
)
@pytest.mark.parametrize(
    "lines", [(1, 1), (1, None), (2, 3), (999_999, None)], ids=["first", "all", "mid", "past-end"]
)
async def test_full_and_ranged_reads_record_the_same_version(tmp_path, content, lines):
    path = tmp_path / "f.txt"
    path.write_bytes(content)
    tracker, read, _, _ = _tools(tmp_path)
    await read.execute(file_path="f.txt")
    whole = tracker.seen(path)
    tracker.forget(path)

    await read.execute(file_path="f.txt", start_line=lines[0], end_line=lines[1])

    assert whole == _sha(content)
    assert tracker.seen(path) == whole


async def test_a_ranged_read_of_a_file_too_large_to_track_reads_only_what_it_needs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(read_mod, "_HASH_FOR_TRACKING_MAX_BYTES", 100_000)
    path = tmp_path / "big.log"
    path.write_bytes(b"".join(b"entry %07d\n" % n for n in range(100_000)))  # 1.4 MB
    real_readinto = read_mod._HashingReader.readinto
    read_bytes = {"total": 0}

    def counting(self, buffer):
        size = real_readinto(self, buffer)
        read_bytes["total"] += size
        return size

    monkeypatch.setattr(read_mod._HashingReader, "readinto", counting)
    tracker, read, _, _ = _tools(tmp_path)
    out = await read.execute(file_path="big.log", start_line=1, end_line=2)

    assert "entry 0000001" in out
    assert tracker.seen(path) is None  # too large to edit, so not tracked, as before
    assert read_bytes["total"] <= 2 * read_mod._READ_CHUNK_BYTES


async def test_a_file_that_grows_past_the_tracking_limit_during_the_read_is_not_recorded(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(read_mod, "_HASH_FOR_TRACKING_MAX_BYTES", 200_000)
    path = tmp_path / "growing.log"
    path.write_bytes(b"x\n" * 50_000)  # 100 KB: under the limit when measured
    real_readinto = read_mod._HashingReader.readinto
    state = {"grown": False, "total": 0}

    def readinto(self, buffer):
        if not state["grown"]:
            state["grown"] = True
            with open(path, "ab") as handle:
                handle.write(b"y\n" * 500_000)  # +1 MB
        size = real_readinto(self, buffer)
        state["total"] += size
        return size

    monkeypatch.setattr(read_mod._HashingReader, "readinto", readinto)
    tracker, read, _, _ = _tools(tmp_path)
    await read.execute(file_path="growing.log", start_line=1, end_line=1)

    assert tracker.seen(path) is None
    assert state["total"] <= 200_000 + 2 * read_mod._READ_CHUNK_BYTES  # never the whole file
