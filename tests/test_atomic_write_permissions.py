"""File modes written by ``atomic_write`` (CodeRabbit #12 on PR #26).

``atomic_write`` creates its temporary file with ``tempfile.mkstemp`` (mode 0o600) and only copied
a mode from an *existing* target, so every new file came out private to its owner, a regression
from the plain ``write_text`` the tools used before (0o666 reduced by the process umask).

Tests that observe real permission bits need POSIX and are skipped elsewhere (Windows has no
umask or mode bits worth asserting; the Linux and macOS CI jobs run them). The code path that
chooses the mode is also checked everywhere, through a seam that stands in for the POSIX
platform and records what the kernel is asked for.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from velune.tools.filesystem import mutation as mut

posix_only = pytest.mark.skipif(os.name != "posix", reason="permission bits and umask are POSIX")


@pytest.fixture
def restore_umask():
    original = os.umask(0)
    os.umask(original)
    yield
    os.umask(original)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith(mut._TMP_PREFIX))


# ── Real permission bits (POSIX) ───────────────────────────────────────────────────────────────


@posix_only
@pytest.mark.parametrize("umask", [0o022, 0o027, 0o077, 0o002, 0o000])
def test_a_new_file_gets_the_mode_a_plain_write_would_give_it(tmp_path, restore_umask, umask):
    os.umask(umask)
    plain = tmp_path / "plain.txt"
    plain.write_text("x")  # what the tools did before atomic_write

    target = tmp_path / "new.txt"
    mut.atomic_write(target, b"hello\n")

    assert _mode(target) == _mode(plain) == 0o666 & ~umask
    assert target.read_bytes() == b"hello\n"


@posix_only
def test_a_new_file_is_never_more_permissive_than_the_umask_allows(tmp_path, restore_umask):
    os.umask(0o077)
    target = tmp_path / "private.txt"
    mut.atomic_write(target, b"secret-ish\n")
    assert _mode(target) & 0o077 == 0
    assert _mode(target) == 0o600


@posix_only
def test_the_mode_is_not_a_hard_coded_default(tmp_path, restore_umask):
    os.umask(0o002)
    target = tmp_path / "group.txt"
    mut.atomic_write(target, b"x")
    assert _mode(target) == 0o664  # not 0o644, not 0o600


@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o664, 0o755, 0o700])
def test_overwriting_an_existing_file_keeps_its_mode(tmp_path, restore_umask, mode):
    os.umask(0o022)
    target = tmp_path / "keep.sh"
    target.write_text("old\n")
    target.chmod(mode)
    mut.atomic_write(target, b"new\n", expected_sha256=mut.file_sha256(target))
    assert _mode(target) == mode
    assert target.read_bytes() == b"new\n"


@posix_only
def test_the_process_umask_is_left_as_it_was(tmp_path, restore_umask):
    os.umask(0o027)
    mut.atomic_write(tmp_path / "a.txt", b"x")
    current = os.umask(0o027)
    assert current == 0o027


@posix_only
async def test_the_agent_tools_create_files_with_the_default_mode(tmp_path, restore_umask):
    from velune.tools.filesystem.write import WriteFile

    os.umask(0o022)
    await WriteFile(workspace=tmp_path, confirm=False).execute(file_path="made.txt", content="x\n")
    assert _mode(tmp_path / "made.txt") == 0o644


@posix_only
def test_the_council_applier_creates_files_with_the_default_mode(tmp_path, restore_umask):
    from velune.execution.edit_formats import EditBlockApplier

    os.umask(0o022)
    EditBlockApplier(tmp_path).write(tmp_path / "council.txt", "x\n")
    assert _mode(tmp_path / "council.txt") == 0o644


# ── The path that picks the mode, on every platform ────────────────────────────────────────────


class _Spy:
    def __init__(self, monkeypatch, *, posix: bool):
        self.opened: list[tuple[str, int, int]] = []  # only atomic_write's own os.open calls
        self.mkstemp_calls = 0
        monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", posix)
        real_open, real_mkstemp = os.open, mut.tempfile.mkstemp

        def open_spy(name, flags, mode=0o777, **kw):
            # ``mkstemp`` calls os.open too (mode 0o600); only record our own request.
            if mode == mut._NEW_FILE_MODE:
                self.opened.append((str(name), flags, mode))
            return real_open(name, flags, mode, **kw)

        def mkstemp_spy(*a, **kw):
            self.mkstemp_calls += 1
            return real_mkstemp(*a, **kw)

        monkeypatch.setattr(mut.os, "open", open_spy)
        monkeypatch.setattr(mut.tempfile, "mkstemp", mkstemp_spy)
        monkeypatch.setattr(
            mut.os, "umask", lambda *_: pytest.fail("atomic_write must not touch the umask")
        )


def test_a_new_file_asks_the_kernel_for_0666_and_lets_it_apply_the_umask(tmp_path, monkeypatch):
    spy = _Spy(monkeypatch, posix=True)
    target = tmp_path / "new.txt"
    mut.atomic_write(target, b"data")
    assert target.read_bytes() == b"data"
    assert spy.mkstemp_calls == 0
    ((name, flags, mode),) = spy.opened
    assert mode == 0o666  # the umask is applied by the kernel, not guessed here
    assert flags & os.O_EXCL and flags & os.O_CREAT
    assert Path(name).parent == tmp_path and Path(name).name.startswith(mut._TMP_PREFIX)
    assert _leftovers(tmp_path) == []  # the temporary file was renamed into place


def test_replacing_an_existing_file_still_copies_its_mode(tmp_path, monkeypatch):
    spy = _Spy(monkeypatch, posix=True)
    target = tmp_path / "old.txt"
    target.write_text("old")
    copied: list[tuple] = []
    real = mut.shutil.copymode
    monkeypatch.setattr(mut.shutil, "copymode", lambda a, b: (copied.append((a, b)), real(a, b)))
    mut.atomic_write(target, b"new", expected_sha256=mut.file_sha256(target))
    assert spy.mkstemp_calls == 1 and spy.opened == []  # existing targets keep the old route
    assert copied and Path(copied[0][0]) == target
    assert target.read_bytes() == b"new"


def test_without_posix_mode_bits_the_file_is_created_as_before(tmp_path, monkeypatch):
    """Windows: no umask is guessed and no permission change is attempted."""
    spy = _Spy(monkeypatch, posix=False)
    chmods: list = []
    monkeypatch.setattr(mut.os, "chmod", lambda *a, **k: chmods.append(a))
    target = tmp_path / "new.txt"
    mut.atomic_write(target, b"data")
    assert target.read_bytes() == b"data"
    assert spy.mkstemp_calls == 1 and spy.opened == []
    assert chmods == []


def test_a_name_collision_picks_another_name(tmp_path, monkeypatch):
    _Spy(monkeypatch, posix=True)
    names = iter(["a" * 32, "a" * 32, "b" * 32])

    class _Id:
        def __init__(self, hex_value):
            self.hex = hex_value

    monkeypatch.setattr(mut.uuid, "uuid4", lambda: _Id(next(names)))
    (tmp_path / f"{mut._TMP_PREFIX}{'a' * 12}.part").write_text("squatter")
    target = tmp_path / "new.txt"
    mut.atomic_write(target, b"data")
    assert target.read_bytes() == b"data"
    assert (tmp_path / f"{mut._TMP_PREFIX}{'a' * 12}.part").read_text() == "squatter"


# ── Atomicity and cleanup are unchanged ────────────────────────────────────────────────────────


@pytest.mark.parametrize("posix", [True, False])
def test_a_failed_swap_leaves_the_original_and_no_temporary_file(tmp_path, monkeypatch, posix):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", posix)
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original")

    def boom(source, destination):
        raise OSError("disk exploded")

    monkeypatch.setattr(mut, "_replace", boom)
    with pytest.raises(OSError, match="disk exploded"):
        mut.atomic_write(target, b"replacement", expected_sha256=mut.file_sha256(target))
    assert target.read_bytes() == b"original"
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("posix", [True, False])
def test_a_failed_new_file_write_creates_nothing(tmp_path, monkeypatch, posix):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", posix)
    monkeypatch.setattr(mut, "_replace", lambda s, d: (_ for _ in ()).throw(OSError("nope")))
    target = tmp_path / "never.txt"
    with pytest.raises(OSError, match="nope"):
        mut.atomic_write(target, b"data")
    assert not target.exists()
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("posix", [True, False])
def test_a_conflict_is_detected_before_the_swap_and_cleans_up(tmp_path, monkeypatch, posix):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", posix)
    target = tmp_path / "f.txt"
    target.write_bytes(b"as the agent saw it")
    seen = mut.file_sha256(target)
    target.write_bytes(b"changed by someone else")
    with pytest.raises(mut.FileConflictError):
        mut.atomic_write(target, b"mine", expected_sha256=seen)
    assert target.read_bytes() == b"changed by someone else"
    assert _leftovers(tmp_path) == []


def test_a_must_not_exist_conflict_leaves_the_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    target = tmp_path / "f.txt"
    target.write_bytes(b"already here")
    with pytest.raises(mut.FileConflictError):
        mut.atomic_write(target, b"mine", expected_sha256=mut.ABSENT)
    assert target.read_bytes() == b"already here"
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("posix", [True, False])
def test_content_is_written_in_full_and_verified(tmp_path, monkeypatch, posix):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", posix)
    payload = os.urandom(1_000_000)
    target = tmp_path / "blob.bin"
    digest = mut.atomic_write(target, payload)
    assert target.read_bytes() == payload
    assert digest == mut.file_sha256(target)
    mut.verify_written(target, digest)
