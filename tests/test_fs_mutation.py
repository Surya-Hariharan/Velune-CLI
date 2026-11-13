"""The shared safe-mutation layer: atomic writes, preserved encodings and line endings,
conflict detection, verification and the per-turn undo journal.

Every assertion is about bytes on disk.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path

import pytest

from velune.tools.filesystem import mutation as mut
from velune.tools.filesystem.edit import EditFile
from velune.tools.filesystem.read import ReadFile
from velune.tools.filesystem.write import CreateFile, DeleteFile, WriteFile


def _tools(ws: Path):
    tracker = mut.FileVersionTracker()
    return (
        ReadFile(workspace=ws, tracker=tracker),
        WriteFile(workspace=ws, confirm=False, tracker=tracker),
        EditFile(workspace=ws, tracker=tracker),
    )


def _leftover_temps(folder: Path) -> list[Path]:
    return list(folder.glob(".velune-tmp-*"))


# ── Atomic writes ──────────────────────────────────────────────────────────────


async def test_failed_replace_leaves_the_original_intact_and_no_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(mut.os, "replace", boom)
    _, write, _ = _tools(tmp_path)
    with pytest.raises(OSError, match="disk full"):
        await write.execute(file_path="keep.txt", content="new content\n")
    assert target.read_bytes() == b"original\n"
    assert _leftover_temps(tmp_path) == []


def test_failure_while_writing_the_temp_file_is_cleaned_up(tmp_path, monkeypatch):
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original")

    def fail_fsync(fd):
        raise OSError("I/O error")

    monkeypatch.setattr(mut.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="I/O error"):
        mut.atomic_write(target, b"partial")
    assert target.read_bytes() == b"original"
    assert _leftover_temps(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_permission_bits_are_preserved(tmp_path):
    target = tmp_path / "script.sh"
    target.write_bytes(b"#!/bin/sh\n")
    target.chmod(0o755)
    mut.atomic_write(target, b"#!/bin/sh\necho hi\n")
    assert target.stat().st_mode & 0o777 == 0o755


def _make_readonly(path: Path) -> None:
    import stat

    path.chmod(stat.S_IREAD)


def _can_write_readonly(tmp_path: Path) -> bool:
    """Root (and some CI containers) ignore the read-only bit, so there is nothing to test."""
    probe = tmp_path / "_probe"
    probe.write_bytes(b"x")
    _make_readonly(probe)
    writable = os.access(probe, os.W_OK)
    probe.chmod(0o644)
    return writable


def test_a_read_only_file_is_refused_with_a_clear_message_and_leaves_no_litter(tmp_path):
    """Regression: on Windows the read-only attribute was copied onto the temp file, the
    swap was refused after a delay, and the temp file could not be deleted — the caller saw
    an error naming the temp file and a `.velune-tmp-*.part` was left in the workspace."""
    if _can_write_readonly(tmp_path):
        pytest.skip("this account ignores the read-only attribute")
    target = tmp_path / "ro.txt"
    target.write_bytes(b"protected\n")
    _make_readonly(target)
    try:
        with pytest.raises(PermissionError, match="read-only") as err:
            mut.atomic_write(target, b"overwritten\n")
        assert ".velune-tmp" not in str(err.value)
        assert target.read_bytes() == b"protected\n"
        assert _leftover_temps(tmp_path) == []
    finally:
        target.chmod(0o644)


def test_a_temp_file_that_inherited_the_read_only_bit_is_still_removed(tmp_path, monkeypatch):
    """The cleanup path itself: a failure after the temp file became read-only."""
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original\n")

    def fail_after_locking_the_temp_file(source, destination):
        _make_readonly(Path(source))
        raise PermissionError("swap refused")

    monkeypatch.setattr(mut, "_replace", fail_after_locking_the_temp_file)
    with pytest.raises(PermissionError, match="swap refused"):
        mut.atomic_write(target, b"new\n")
    assert target.read_bytes() == b"original\n"
    assert _leftover_temps(tmp_path) == []


@pytest.mark.skipif(os.name != "nt", reason="reserved device names exist only on Windows")
@pytest.mark.parametrize("name", ["CON", "nul.txt", "Com1.log", "LPT9", "aux.", "PRN .md"])
def test_windows_device_names_are_refused_and_nothing_is_created(tmp_path, name):
    """Regression: `write_file CON` really created a file Windows tools cannot delete."""
    before = {p.name for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="reserved Windows device name"):
        mut.atomic_write(tmp_path / name, b"x")
    assert {p.name for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    "name", ["console.txt", "nullable.py", "com10.txt", "lpt.txt", "auxiliary"]
)
def test_names_that_merely_resemble_device_names_are_fine(tmp_path, name):
    mut.atomic_write(tmp_path / name, b"ok")
    assert (tmp_path / name).read_bytes() == b"ok"


def test_expected_absent_refuses_to_clobber_an_existing_file(tmp_path):
    target = tmp_path / "a.txt"
    target.write_bytes(b"someone else's")
    with pytest.raises(mut.FileConflictError):
        mut.atomic_write(target, b"mine", expected_sha256=mut.ABSENT)
    assert target.read_bytes() == b"someone else's"


# ── Line endings and encodings ─────────────────────────────────────────────────


async def test_crlf_file_stays_crlf_after_a_one_line_edit(tmp_path):
    target = tmp_path / "win.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    _, _, edit = _tools(tmp_path)
    out = await edit.execute(file_path="win.txt", old_string="beta", new_string="BETA")
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"
    assert out.startswith("edit_file OK win.txt (+1 -1) verified")


async def test_multiline_lf_edit_matches_and_writes_crlf_in_a_crlf_file(tmp_path):
    target = tmp_path / "win.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    _, _, edit = _tools(tmp_path)
    await edit.execute(file_path="win.txt", old_string="one\ntwo\n", new_string="1\n2\n")
    assert target.read_bytes() == b"1\r\n2\r\nthree\r\n"


async def test_write_file_keeps_crlf_and_lf_files_in_their_style(tmp_path):
    crlf, lf = tmp_path / "crlf.txt", tmp_path / "lf.txt"
    crlf.write_bytes(b"a\r\nb\r\n")
    lf.write_bytes(b"a\nb\n")
    _, write, _ = _tools(tmp_path)
    await write.execute(file_path="crlf.txt", content="a\nb\nc\n")
    await write.execute(file_path="lf.txt", content="a\nb\nc\n")
    assert crlf.read_bytes() == b"a\r\nb\r\nc\r\n"
    assert lf.read_bytes() == b"a\nb\nc\n"


async def test_new_file_is_written_exactly_as_given_on_every_platform(tmp_path):
    _, write, _ = _tools(tmp_path)
    await write.execute(file_path="new.txt", content="x\ny\n")
    # Path.write_text would have produced b"x\r\ny\r\n" on Windows.
    assert (tmp_path / "new.txt").read_bytes() == b"x\ny\n"


async def test_mixed_line_endings_outside_the_edit_are_untouched(tmp_path):
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"a\r\nb\nc\r\nd\n")
    _, _, edit = _tools(tmp_path)
    await edit.execute(file_path="mixed.txt", old_string="c", new_string="C")
    assert target.read_bytes() == b"a\r\nb\nC\r\nd\n"


async def test_missing_trailing_newline_is_preserved(tmp_path):
    target = tmp_path / "no_eol.txt"
    target.write_bytes(b"first\nlast")
    _, _, edit = _tools(tmp_path)
    await edit.execute(file_path="no_eol.txt", old_string="first", new_string="FIRST")
    assert target.read_bytes() == b"FIRST\nlast"


async def test_utf8_bom_is_preserved(tmp_path):
    target = tmp_path / "bom.txt"
    target.write_bytes(codecs.BOM_UTF8 + "héllo\n".encode())
    _, _, edit = _tools(tmp_path)
    await edit.execute(file_path="bom.txt", old_string="héllo", new_string="wörld")
    assert target.read_bytes() == codecs.BOM_UTF8 + "wörld\n".encode()


async def test_utf16_file_keeps_its_encoding_and_bom(tmp_path):
    target = tmp_path / "u16.txt"
    target.write_bytes(codecs.BOM_UTF16_LE + "line one\r\nline two\r\n".encode("utf-16-le"))
    _, _, edit = _tools(tmp_path)
    await edit.execute(file_path="u16.txt", old_string="two", new_string="2")
    assert target.read_bytes() == codecs.BOM_UTF16_LE + "line one\r\nline 2\r\n".encode("utf-16-le")


async def test_unicode_round_trip(tmp_path):
    target = tmp_path / "uni.py"
    target.write_text("greeting = '你好'  # 🚀\n", encoding="utf-8", newline="")
    _, _, edit = _tools(tmp_path)
    await edit.execute(file_path="uni.py", old_string="你好", new_string="こんにちは")
    assert target.read_text(encoding="utf-8") == "greeting = 'こんにちは'  # 🚀\n"


async def test_characters_the_files_encoding_cannot_hold_are_refused(tmp_path):
    target = tmp_path / "legacy.txt"
    target.write_bytes("caf\xe9\n".encode("cp1252"))
    _, _, edit = _tools(tmp_path)
    with pytest.raises(ValueError, match="cannot be saved"):
        await edit.execute(file_path="legacy.txt", old_string="caf", new_string="🚀")
    assert target.read_bytes() == b"caf\xe9\n"


# ── Conflict detection ─────────────────────────────────────────────────────────


async def test_change_after_read_is_a_conflict_until_reread(tmp_path):
    target = tmp_path / "shared.py"
    target.write_bytes(b"x = 1\n")
    read, write, edit = _tools(tmp_path)
    await read.execute(file_path="shared.py")

    target.write_bytes(b"x = 1\ny = 2  # the user's edit\n")  # someone else edits it

    with pytest.raises(mut.FileConflictError, match="changed on disk"):
        await edit.execute(file_path="shared.py", old_string="x = 1", new_string="x = 10")
    with pytest.raises(mut.FileConflictError):
        await write.execute(file_path="shared.py", content="x = 10\n")
    assert target.read_bytes() == b"x = 1\ny = 2  # the user's edit\n"

    await read.execute(file_path="shared.py")  # re-read, then retry
    await edit.execute(file_path="shared.py", old_string="x = 1", new_string="x = 10")
    assert target.read_bytes() == b"x = 10\ny = 2  # the user's edit\n"


async def test_ranged_reads_also_establish_the_version(tmp_path):
    target = tmp_path / "big.txt"
    target.write_bytes(b"".join(f"line {i}\n".encode() for i in range(100)))
    read, _, edit = _tools(tmp_path)
    await read.execute(file_path="big.txt", start_line=10, end_line=12)
    target.write_bytes(target.read_bytes() + b"appended\n")
    with pytest.raises(mut.FileConflictError):
        await edit.execute(file_path="big.txt", old_string="line 11\n", new_string="eleven\n")


async def test_explicit_expected_sha256_is_enforced(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"v1\n")
    _, write, edit = _tools(tmp_path)
    with pytest.raises(mut.FileConflictError):
        await edit.execute(
            file_path="f.txt", old_string="v1", new_string="v2", expected_sha256="0" * 64
        )
    good = mut.sha256_bytes(b"v1\n")
    out = await write.execute(file_path="f.txt", content="v2\n", expected_sha256=good)
    assert target.read_bytes() == b"v2\n"
    new_sha = mut.sha256_bytes(b"v2\n")
    assert f"sha256={new_sha}" in out


async def test_agents_own_writes_are_not_conflicts(tmp_path):
    read, write, edit = _tools(tmp_path)
    await write.execute(file_path="f.txt", content="a\n")
    await edit.execute(file_path="f.txt", old_string="a", new_string="b")
    await write.execute(file_path="f.txt", content="c\n")
    assert await read.execute(file_path="f.txt") == "c\n"


def test_change_between_check_and_swap_is_caught(tmp_path, monkeypatch):
    target = tmp_path / "race.txt"
    target.write_bytes(b"seen\n")
    seen = mut.sha256_bytes(b"seen\n")
    real_fsync = mut.os.fsync

    def fsync_then_someone_writes(fd):
        real_fsync(fd)
        target.write_bytes(b"theirs\n")

    monkeypatch.setattr(mut.os, "fsync", fsync_then_someone_writes)
    with pytest.raises(mut.FileConflictError):
        mut.atomic_write(target, b"mine\n", expected_sha256=seen)
    assert target.read_bytes() == b"theirs\n"
    assert _leftover_temps(tmp_path) == []


# ── Verification ───────────────────────────────────────────────────────────────


async def test_a_write_that_does_not_land_is_not_reported_as_success(tmp_path, monkeypatch):
    _, write, _ = _tools(tmp_path)
    # The swap "succeeds" but nothing reaches the target (e.g. a filter driver ate it).
    monkeypatch.setattr(mut.os, "replace", lambda src, dst: None)
    with pytest.raises(mut.MutationVerificationError):
        await write.execute(file_path="v.txt", content="hello\n")


async def test_results_carry_a_unified_diff(tmp_path):
    (tmp_path / "d.py").write_bytes(b"def f():\n    return 1\n")
    _, _, edit = _tools(tmp_path)
    out = await edit.execute(file_path="d.py", old_string="return 1", new_string="return 2")
    assert "--- a/d.py" in out and "+++ b/d.py" in out
    assert "-    return 1" in out and "+    return 2" in out


# ── create / delete ────────────────────────────────────────────────────────────


async def test_create_file_refuses_to_clobber(tmp_path):
    (tmp_path / "exists.txt").write_bytes(b"keep")
    tool = CreateFile(workspace=tmp_path, confirm=False)
    with pytest.raises(FileExistsError):
        await tool.execute(file_path="exists.txt")
    assert (tmp_path / "exists.txt").read_bytes() == b"keep"
    out = await tool.execute(file_path="sub/new.txt")
    assert (tmp_path / "sub" / "new.txt").read_bytes() == b""
    assert out.startswith("create_file OK sub/new.txt")


async def test_delete_file_refuses_folders_and_verifies(tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "gone.txt").write_bytes(b"x\ny\n")
    tool = DeleteFile(workspace=tmp_path, confirm=False)
    with pytest.raises(IsADirectoryError):
        await tool.execute(file_path="folder")
    out = await tool.execute(file_path="gone.txt")
    assert not (tmp_path / "gone.txt").exists()
    assert out.startswith("delete_file OK gone.txt (+0 -2) verified")


# ── Undo journal ───────────────────────────────────────────────────────────────


async def test_rewind_restores_every_kind_of_change(tmp_path):
    from velune.tools.filesystem.dirs import CreateDirectory, DeleteDirectory, MovePath

    (tmp_path / "edit.txt").write_bytes(b"before\r\n")
    (tmp_path / "remove.txt").write_bytes(b"keep me")
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "inner.txt").write_bytes(b"inner")
    (tmp_path / "moveme.txt").write_bytes(b"moving")
    original = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        _, write, edit = _tools(tmp_path)
        await edit.execute(file_path="edit.txt", old_string="before", new_string="after")
        await write.execute(file_path="created.txt", content="new\n")
        await DeleteFile(workspace=tmp_path, confirm=False).execute(file_path="remove.txt")
        await DeleteDirectory(workspace=tmp_path).execute(path="old", recursive=True)
        await CreateDirectory(workspace=tmp_path).execute(path="made/deep")
        await MovePath(workspace=tmp_path).execute(source="moveme.txt", destination="moved/m.txt")
    finally:
        mut.reset_active_journal(token)

    # edit, create, delete, rmtree, 2 mkdirs, the move's new parent folder, the move
    assert len(journal.describe()) == 8
    report = journal.rewind()
    assert report.skipped == []
    after = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == original
    assert not (tmp_path / "made").exists()
    assert not (tmp_path / "moved").exists()


async def test_rewind_never_overwrites_a_file_changed_since_the_turn(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"v1\n")
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        _, _, edit = _tools(tmp_path)
        await edit.execute(file_path="f.txt", old_string="v1", new_string="v2")
    finally:
        mut.reset_active_journal(token)
    (tmp_path / "f.txt").write_bytes(b"the user's v3\n")
    report = journal.rewind()
    assert report.restored == []
    assert len(report.skipped) == 1
    assert (tmp_path / "f.txt").read_bytes() == b"the user's v3\n"


async def test_no_active_journal_means_nothing_is_recorded(tmp_path):
    _, write, _ = _tools(tmp_path)
    await write.execute(file_path="x.txt", content="x")
    assert mut.active_journal() is None
