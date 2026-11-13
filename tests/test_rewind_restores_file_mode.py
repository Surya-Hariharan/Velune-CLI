"""``/rewind`` gives a deleted file back with the permissions it had (CodeRabbit follow-up on #12).

New files now take the umask-derived default instead of ``mkstemp``'s 0600. Restoring a *deleted*
file went through the same path, so a private 0600 file came back readable by other users. The
journal now records the mode of a file it deletes and the restore sets it before any content is
written. Genuine creations keep the umask-derived default.

Tests on real permission bits need POSIX (the Linux and macOS CI jobs run them); the logic is also
checked everywhere through the same seam ``test_atomic_write_permissions`` uses.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from velune.execution.edit_formats import EditBlockApplier
from velune.tools.filesystem import mutation as mut
from velune.tools.filesystem.dirs import MovePath
from velune.tools.filesystem.write import DeleteFile, WriteFile

posix_only = pytest.mark.skipif(os.name != "posix", reason="permission bits and umask are POSIX")


@pytest.fixture
def restore_umask():
    original = os.umask(0)
    os.umask(original)
    yield
    os.umask(original)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _journaled(work):
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        work()
    finally:
        mut.reset_active_journal(token)
    return journal


async def _journaled_async(work):
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        await work()
    finally:
        mut.reset_active_journal(token)
    return journal


# ── Real permission bits (POSIX) ───────────────────────────────────────────────────────────────


@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o755, 0o400])
async def test_rewinding_a_delete_gives_the_file_back_its_mode(tmp_path, restore_umask, mode):
    os.umask(0o022)
    secret = tmp_path / "id_key"
    secret.write_text("private\n")
    secret.chmod(mode)

    journal = await _journaled_async(
        lambda: DeleteFile(workspace=tmp_path, confirm=False).execute(file_path="id_key")
    )
    assert not secret.exists()
    report = journal.rewind()

    assert report.restored == [str(secret)]
    assert secret.read_text() == "private\n"
    assert _mode(secret) == mode, f"{oct(_mode(secret))} != {oct(mode)}"


@posix_only
@pytest.mark.parametrize("final_mode", [0o600, 0o644, 0o755])
def test_the_temporary_file_is_owner_only_from_the_moment_it_exists(
    tmp_path, restore_umask, monkeypatch, final_mode
):
    """A reader who opens the temporary file early keeps access even after a later chmod.

    So it must start private (0600, whatever the umask), receive its data, and only then be
    widened to the recorded mode. Checked at the instant of creation, with a permissive umask.
    """
    os.umask(0o022)  # a plain create would make the file 0644 and world-readable
    secret = tmp_path / "token"
    secret.write_text("s3cret\n")
    secret.chmod(final_mode)
    journal = _journaled(lambda: EditBlockApplier(tmp_path).write(secret, ""))
    assert not secret.exists()

    created: list[int] = []
    real_open = os.open

    def spy(name, flags, mode=0o777, **kw):
        fd = real_open(name, flags, mode, **kw)
        if flags & os.O_CREAT and mut._TMP_PREFIX in str(name):
            created.append(stat.S_IMODE(os.fstat(fd).st_mode))  # mode at the instant of creation
        return fd

    monkeypatch.setattr(os, "open", spy)
    journal.rewind()
    assert created == [0o600], [oct(m) for m in created]
    assert secret.read_text() == "s3cret\n"
    assert _mode(secret) == final_mode  # widened (or kept) only after the content was in


@posix_only
def test_the_council_applier_delete_is_restored_with_its_mode(tmp_path, restore_umask):
    os.umask(0o022)
    target = tmp_path / "private.cfg"
    target.write_text("x\n")
    target.chmod(0o600)
    journal = _journaled(lambda: EditBlockApplier(tmp_path).write(target, ""))
    journal.rewind()
    assert target.read_text() == "x\n" and _mode(target) == 0o600


@posix_only
async def test_a_file_replaced_by_a_move_is_restored_with_its_mode(tmp_path, restore_umask):
    os.umask(0o022)
    (tmp_path / "src.txt").write_text("new\n")
    dst = tmp_path / "dst.txt"
    dst.write_text("old private\n")
    dst.chmod(0o600)
    journal = await _journaled_async(
        lambda: MovePath(workspace=tmp_path).execute(
            source="src.txt", destination="dst.txt", overwrite=True
        )
    )
    journal.rewind()
    assert dst.read_text() == "old private\n"
    assert _mode(dst) == 0o600


@posix_only
async def test_undoing_an_overwrite_keeps_the_mode_of_the_file(tmp_path, restore_umask):
    os.umask(0o022)
    target = tmp_path / "script.sh"
    target.write_text("echo one\n")
    target.chmod(0o750)
    journal = await _journaled_async(
        lambda: WriteFile(workspace=tmp_path, confirm=False).execute(
            file_path="script.sh", content="echo two\n"
        )
    )
    assert _mode(target) == 0o750
    journal.rewind()
    assert target.read_text() == "echo one\n" and _mode(target) == 0o750


@posix_only
async def test_undoing_a_creation_and_creating_files_still_follow_the_umask(
    tmp_path, restore_umask
):
    os.umask(0o027)
    journal = await _journaled_async(
        lambda: WriteFile(workspace=tmp_path, confirm=False).execute(
            file_path="fresh.txt", content="x\n"
        )
    )
    assert _mode(tmp_path / "fresh.txt") == 0o640  # umask-derived, not a hard-coded mode
    journal.rewind()
    assert not (tmp_path / "fresh.txt").exists()


@posix_only
def test_setuid_and_sticky_bits_are_never_restored(tmp_path, restore_umask):
    os.umask(0o022)
    target = tmp_path / "tool"
    target.write_text("x\n")
    target.chmod(0o4755)
    assert mut.file_mode(target) == 0o755


# ── The logic, on every platform ───────────────────────────────────────────────────────────────


def test_a_deleted_files_mode_is_recorded_in_the_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    target = tmp_path / "f.txt"
    target.write_text("x\n")
    journal = _journaled(lambda: EditBlockApplier(tmp_path).write(target, ""))
    (entry,) = journal.entries
    assert entry.after_sha == mut.ABSENT
    assert entry.mode is not None


def test_a_new_file_with_an_explicit_mode_is_created_private_and_widened_after_the_data(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    opened: list[int] = []
    real_open = os.open

    def open_spy(name, flags, mode=0o777, **kw):
        if mut._TMP_PREFIX in str(name):
            opened.append(mode)  # what the kernel is asked to create the temporary file with
        return real_open(name, flags, mode, **kw)

    monkeypatch.setattr(mut.os, "open", open_spy)
    chmods: list[tuple[int, int]] = []
    monkeypatch.setattr(
        mut.os,
        "fchmod",
        lambda fd, mode: chmods.append((os.fstat(fd).st_size, mode)),
        raising=False,
    )
    mut.atomic_write(tmp_path / "new.txt", b"secret", expected_sha256=mut.ABSENT, mode=0o644)
    assert opened == [0o600]  # owner-only from the first instant, never 0o666
    assert chmods == [(6, 0o644)]  # widened afterwards, with all 6 bytes already written
    assert (tmp_path / "new.txt").read_bytes() == b"secret"


def test_no_mode_is_applied_for_an_ordinary_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    calls: list = []
    monkeypatch.setattr(mut.os, "fchmod", lambda *a: calls.append(a), raising=False)
    mut.atomic_write(tmp_path / "new.txt", b"x")
    assert calls == []  # the umask-derived default stands


def test_the_mode_is_ignored_when_replacing_an_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    calls: list = []
    monkeypatch.setattr(mut.os, "fchmod", lambda *a: calls.append(a), raising=False)
    target = tmp_path / "exists.txt"
    target.write_text("old")
    mut.atomic_write(target, b"new", expected_sha256=mut.file_sha256(target), mode=0o600)
    assert calls == []  # an existing file keeps the mode it has
    assert target.read_bytes() == b"new"


def test_without_posix_modes_nothing_is_recorded_or_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", False)
    calls: list = []
    monkeypatch.setattr(mut.os, "fchmod", lambda *a: calls.append(a), raising=False)
    target = tmp_path / "f.txt"
    target.write_text("x")
    assert mut.file_mode(target) is None
    mut.atomic_write(tmp_path / "g.txt", b"x", expected_sha256=mut.ABSENT, mode=0o600)
    assert calls == []


def test_a_missing_file_has_no_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    assert mut.file_mode(tmp_path / "absent") is None


def test_old_journal_entries_without_a_mode_still_rewind(tmp_path):
    """FileChange(path, before, after) as built before the mode existed."""
    target = tmp_path / "f.txt"
    journal = mut.TurnJournal()
    journal.add(mut.FileChange(target, b"hello", mut.ABSENT))
    report = journal.rewind()
    assert target.read_bytes() == b"hello" and report.restored == [str(target)]
