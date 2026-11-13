"""Oversized files are never read whole, encodings are never silently converted (#39), and
rewinding a recursive folder deletion gives each file back its permissions (#41).

#39: ``write_file`` treated "over the size limit" like "binary", read the whole file with no
bound, kept a before-image larger than the journal limit, and rewrote a UTF-16 file as UTF-8.
The approval preview also read the whole file before anything was approved.

#41: a deleted folder's snapshot kept paths and bytes only, and the rewind wrote files with
``write_bytes``, so a 0600 file came back with umask-derived permissions (0644).

Real permission bits need POSIX (the Linux and macOS CI jobs run those tests). The logic is
also checked on every platform through the same seams ``test_rewind_restores_file_mode``
uses. New names are looked up through ``mut.`` when a test runs, so on an implementation
without them each test fails on its own instead of the whole module failing to import.
"""

from __future__ import annotations

import codecs
import io
import os
import stat
import tracemalloc
from pathlib import Path

import pytest

from velune.execution.diff_preview import DiffDecision, DiffPreview, compute_file_diff
from velune.tools.filesystem import mutation as mut
from velune.tools.filesystem.dirs import DeleteDirectory
from velune.tools.filesystem.write import WriteFile

posix_only = pytest.mark.skipif(os.name != "posix", reason="permission bits and umask are POSIX")

MiB = 1024 * 1024
#: Small limits for the boundary tests; above the 64 KiB sample read of an oversized file.
LIMIT = 200_000


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(mut, "MAX_MUTATION_BYTES", LIMIT)
    monkeypatch.setattr(mut, "JOURNAL_MAX_BYTES", LIMIT)


@pytest.fixture
def restore_umask():
    original = os.umask(0)
    os.umask(original)
    yield
    os.umask(original)


class ReadMeter:
    """Counts the bytes the mutation module reads, and the largest single read."""

    def __init__(self, monkeypatch) -> None:
        self.total = 0
        self.largest = 0
        real_open = open
        meter = self

        class Handle:
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                data = self._handle.read(size)
                meter.total += len(data)
                meter.largest = max(meter.largest, len(data))
                return data

            def __getattr__(self, name):
                return getattr(self._handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._handle.__exit__(*exc)

        def spy(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            return Handle(handle) if "r" in mode and "b" in mode else handle

        monkeypatch.setattr(mut, "open", spy, raising=False)

        def refuse(self, *a, **k):  # a whole-file read anywhere is the defect being guarded
            raise AssertionError(f"unbounded whole-file read of {self}")

        monkeypatch.setattr(Path, "read_bytes", refuse)
        monkeypatch.setattr(Path, "read_text", refuse)


async def _write(ws: Path, name: str, content: str):
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        out = await WriteFile(workspace=ws, confirm=False).execute(file_path=name, content=content)
    finally:
        mut.reset_active_journal(token)
    return journal, out


def _utf16(text: str) -> bytes:
    return codecs.BOM_UTF16_LE + text.encode("utf-16-le")


def _no_temp_files(root: Path) -> bool:
    return not list(root.rglob(f"{mut._TMP_PREFIX}*"))


# ── #39: the size limit, at its boundaries ────────────────────────────────────


@pytest.mark.parametrize("size", [LIMIT - 1, LIMIT])
async def test_text_up_to_the_limit_keeps_its_encoding_and_is_rewindable(
    tmp_path, small_limits, size
):
    raw = b"plain line\n" * ((size - 3) // 11)
    raw += b"a" * (size - 3 - len(raw)) + "é\n".encode()  # valid UTF-8, exactly *size* bytes
    (tmp_path / "f.txt").write_bytes(raw)
    assert (tmp_path / "f.txt").stat().st_size == size

    journal, _ = await _write(tmp_path, "f.txt", "new\ncontent\n")
    assert (tmp_path / "f.txt").read_bytes() == b"new\ncontent\n"
    (entry,) = journal.entries
    assert entry.before == raw and journal.irreversible == []
    journal.rewind()
    assert (tmp_path / "f.txt").read_bytes() == raw


async def test_text_just_over_the_limit_is_refused_and_left_untouched(tmp_path, small_limits):
    raw = b"plain text line\n" * (LIMIT // 16) + b"x" * (LIMIT % 16 + 1)
    (tmp_path / "f.txt").write_bytes(raw)
    assert (tmp_path / "f.txt").stat().st_size == LIMIT + 1

    with pytest.raises(mut.FileTooLargeError, match="encoding cannot be confirmed"):
        await _write(tmp_path, "f.txt", "replacement\n")
    assert (tmp_path / "f.txt").read_bytes() == raw  # never truncated, never converted
    assert _no_temp_files(tmp_path)


async def test_an_oversized_file_is_never_read_whole(tmp_path, small_limits, monkeypatch):
    raw = _utf16("línea\r\n" * (LIMIT // 7))
    (tmp_path / "log.txt").write_bytes(raw)
    meter = ReadMeter(monkeypatch)

    await _write(tmp_path, "log.txt", "replaced\n")

    # Content is only ever read through a bounded sample; whole-file reads would have
    # raised. Hashing for conflict detection streams in chunks of at most 1 MiB.
    assert meter.largest <= max(mut._SAMPLE_BYTES, mut._HASH_CHUNK)


def test_a_bounded_read_never_reads_past_its_limit_even_if_the_file_grows(tmp_path, monkeypatch):
    path = tmp_path / "grows.txt"
    path.write_bytes(b"x" * 5000)
    real_fstat = os.fstat
    # The file "was" small when it was measured and has grown since.
    monkeypatch.setattr(
        mut.os, "fstat", lambda fd: os.stat_result((*real_fstat(fd)[:6], 10, *real_fstat(fd)[7:]))
    )
    meter = ReadMeter(monkeypatch)
    with pytest.raises(mut.FileTooLargeError):
        mut.read_bounded(path, 1000)
    assert meter.total <= 1001


def test_a_file_already_over_the_limit_is_refused_before_any_byte_is_read(tmp_path, monkeypatch):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * 5000)
    meter = ReadMeter(monkeypatch)
    with pytest.raises(mut.FileTooLargeError) as caught:
        mut.read_bounded(path, 1000)
    assert meter.total == 0
    assert caught.value.size == 5000 and caught.value.limit == 1000


def test_the_three_conditions_are_told_apart(tmp_path, small_limits):
    (tmp_path / "big.txt").write_bytes(b"a" * (LIMIT + 1))
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "bad.txt").write_bytes(b"caf\xc3 \x81\x90 text\n")  # not UTF-8, not cp1252
    with pytest.raises(mut.FileTooLargeError):
        mut.load_text(tmp_path / "big.txt")
    with pytest.raises(mut.BinaryFileError):
        mut.load_text(tmp_path / "blob.bin")
    with pytest.raises(mut.TextEncodingError):
        mut.load_text(tmp_path / "bad.txt")
    # All three are still ValueErrors, so existing callers keep working.
    assert all(
        issubclass(e, ValueError)
        for e in (mut.FileTooLargeError, mut.BinaryFileError, mut.TextEncodingError)
    )


# ── #39: encodings ───────────────────────────────────────────────────────────


async def test_an_oversized_utf16_file_keeps_its_bom_encoding_and_line_endings(
    tmp_path, small_limits
):
    (tmp_path / "log.txt").write_bytes(_utf16("línea de registro\r\n" * (LIMIT // 36 + 10)))
    assert (tmp_path / "log.txt").stat().st_size > LIMIT

    journal, out = await _write(tmp_path, "log.txt", "première\nseconde\n")

    assert (tmp_path / "log.txt").read_bytes() == _utf16("première\r\nseconde\r\n")
    assert "kept its utf-16-le encoding" in out
    assert "/rewind cannot restore" in out


async def test_a_large_real_utf16_file_is_not_rewritten_as_utf8_or_loaded_into_memory(tmp_path):
    """The reproduction from the issue: a 22 MiB UTF-16 file, real limits."""
    chunk = "línea de registro\n".encode("utf-16-le") * 4096
    with open(tmp_path / "big.txt", "wb") as handle:
        handle.write(codecs.BOM_UTF16_LE)
        for _ in range(22 * MiB // len(chunk) + 1):
            handle.write(chunk)
    size = (tmp_path / "big.txt").stat().st_size
    assert size > mut.MAX_MUTATION_BYTES

    tracemalloc.start()
    try:
        journal, _ = await _write(tmp_path, "big.txt", "replaced\n")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < 8 * MiB, f"peak {peak / MiB:.1f} MiB for a {size / MiB:.0f} MiB file"
    assert (tmp_path / "big.txt").read_bytes() == _utf16("replaced\n")
    assert journal.entries == [] and journal.irreversible == [f"overwrote {tmp_path / 'big.txt'}"]


async def test_utf16_within_the_limit_keeps_its_encoding(tmp_path, small_limits):
    (tmp_path / "w.txt").write_bytes(_utf16("uno\r\ndos\r\n"))
    await _write(tmp_path, "w.txt", "tres\n")
    assert (tmp_path / "w.txt").read_bytes() == _utf16("tres\r\n")


async def test_undecodable_text_is_refused_not_overwritten_in_another_encoding(tmp_path):
    raw = b"caf\xc3 \x81\x90 text\n"
    (tmp_path / "bad.txt").write_bytes(raw)
    with pytest.raises(mut.TextEncodingError):
        await _write(tmp_path, "bad.txt", "new\n")
    assert (tmp_path / "bad.txt").read_bytes() == raw


async def test_an_oversized_file_with_a_bom_but_corrupt_text_is_refused(tmp_path, small_limits):
    raw = codecs.BOM_UTF16_LE + b"\x00\xd8" + b"a\x00" * LIMIT  # a lone surrogate first
    (tmp_path / "bad.txt").write_bytes(raw)
    with pytest.raises(mut.TextEncodingError):
        await _write(tmp_path, "bad.txt", "new\n")
    assert (tmp_path / "bad.txt").stat().st_size == len(raw)


# ── #39: binary files keep their behaviour ───────────────────────────────────


async def test_a_binary_file_within_the_limit_is_replaced_and_rewindable(tmp_path, small_limits):
    raw = b"\x00\x01\x02" * 1000
    (tmp_path / "blob.bin").write_bytes(raw)
    journal, _ = await _write(tmp_path, "blob.bin", "now text\n")
    assert (tmp_path / "blob.bin").read_bytes() == b"now text\n"
    journal.rewind()
    assert (tmp_path / "blob.bin").read_bytes() == raw


async def test_an_oversized_binary_file_is_replaced_and_marked_irreversible(tmp_path, small_limits):
    (tmp_path / "blob.bin").write_bytes(b"\x00" * (LIMIT + 1))
    journal, out = await _write(tmp_path, "blob.bin", "now text\n")
    assert (tmp_path / "blob.bin").read_bytes() == b"now text\n"
    assert journal.entries == [] and journal.irreversible == [f"overwrote {tmp_path / 'blob.bin'}"]
    assert "/rewind cannot restore" in out
    report = journal.rewind()
    assert (tmp_path / "blob.bin").exists(), "/rewind must not delete a file it could not back up"
    assert report.restored == [] and "cannot be rewound" in report.skipped[0]


@pytest.mark.skipif(os.name == "nt", reason="NTFS does not create sparse files on truncate")
async def test_a_very_large_sparse_file_is_handled_with_bounded_memory(tmp_path):
    with open(tmp_path / "sparse.img", "wb") as handle:
        handle.truncate(256 * MiB)
    tracemalloc.start()
    try:
        journal, _ = await _write(tmp_path, "sparse.img", "small\n")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * MiB, f"peak {peak / MiB:.1f} MiB"
    assert (tmp_path / "sparse.img").read_bytes() == b"small\n"
    assert journal.irreversible == [f"overwrote {tmp_path / 'sparse.img'}"]


# ── #39: the journal limit is enforced on its own ────────────────────────────


async def test_the_journal_limit_applies_even_below_the_editing_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "MAX_MUTATION_BYTES", LIMIT)
    monkeypatch.setattr(mut, "JOURNAL_MAX_BYTES", 1000)
    raw = "línea\r\n".encode() * 1000  # 8000 bytes: editable, too large to keep
    (tmp_path / "f.txt").write_bytes(raw)

    journal, out = await _write(tmp_path, "f.txt", "nueva\n")

    assert (tmp_path / "f.txt").read_bytes() == b"nueva\r\n"  # style kept
    assert journal.entries == [] and journal.irreversible == [f"overwrote {tmp_path / 'f.txt'}"]
    assert "/rewind cannot restore" in out
    report = journal.rewind()
    assert (tmp_path / "f.txt").exists()
    assert report.restored == []


def test_a_before_image_is_never_read_past_the_journal_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "JOURNAL_MAX_BYTES", 1000)
    (tmp_path / "f.bin").write_bytes(b"x" * 5000)
    meter = ReadMeter(monkeypatch)
    assert mut.read_before_image(tmp_path / "f.bin") == (None, False)
    assert meter.total == 0


# ── #39: nothing is read whole just to draw the approval preview ─────────────


def test_the_approval_preview_does_not_read_an_oversized_file(tmp_path, small_limits, monkeypatch):
    (tmp_path / "big.txt").write_bytes(b"text\n" * (LIMIT // 5 + 1))
    meter = ReadMeter(monkeypatch)
    with pytest.raises(mut.FileTooLargeError):
        compute_file_diff(tmp_path / "big.txt", "replacement\n")
    assert meter.total == 0


def test_the_approval_preview_still_shows_files_within_the_limit(tmp_path, small_limits):
    (tmp_path / "f.txt").write_bytes(b"one\r\ntwo\r\n")
    diff = compute_file_diff(tmp_path / "f.txt", "one\ntwo\nthree\n")
    assert diff.original == "one\ntwo\n" and not diff.is_new_file


async def test_the_confirm_prompt_still_asks_for_an_oversized_file_without_a_preview(
    tmp_path, small_limits
):
    from rich.console import Console

    (tmp_path / "big.txt").write_bytes(b"text\n" * (LIMIT // 5 + 1))
    out = io.StringIO()
    preview = DiffPreview(Console(file=out, width=200))
    decision = await preview.preview_and_confirm(tmp_path / "big.txt", "", auto_accept=True)
    assert decision == DiffDecision.ACCEPT
    assert "No preview is shown" in out.getvalue()


async def test_the_council_batch_preview_handles_an_oversized_file_without_reading_it(
    tmp_path, small_limits, monkeypatch
):
    from rich.console import Console

    from velune.execution.multi_diff import MultiDiffPreview

    big = tmp_path / "big.txt"
    big.write_bytes(b"text\n" * (LIMIT // 5 + 1))
    out = io.StringIO()
    batch = MultiDiffPreview(Console(file=out, width=200))
    meter = ReadMeter(monkeypatch)
    decisions = await batch.preview_batch({big: "new\n"}, auto_accept=True)
    assert decisions == {big: DiffDecision.ACCEPT}
    assert "No preview is shown" in out.getvalue()
    assert meter.total == 0


async def test_an_unsafe_write_is_refused_before_the_approval_prompt(
    tmp_path, small_limits, monkeypatch
):
    (tmp_path / "big.txt").write_bytes(b"text\n" * (LIMIT // 5 + 1))

    async def never(*a, **k):
        raise AssertionError("the approval prompt was reached")

    monkeypatch.setattr(DiffPreview, "preview_and_confirm", never)
    with pytest.raises(mut.FileTooLargeError):
        await WriteFile(workspace=tmp_path, confirm=True).execute(
            file_path="big.txt", content="x\n"
        )


async def test_conflict_detection_still_applies_to_an_oversized_file(tmp_path, small_limits):
    (tmp_path / "big.bin").write_bytes(b"\x00" * (LIMIT + 1))
    tool = WriteFile(workspace=tmp_path, confirm=False)
    with pytest.raises(mut.FileConflictError):
        await tool.execute(file_path="big.bin", content="x\n", expected_sha256="0" * 64)
    assert (tmp_path / "big.bin").stat().st_size == LIMIT + 1


# ── #41: real permission bits (POSIX) ────────────────────────────────────────


def _tree(root: Path, files: dict[str, int]) -> None:
    for rel, mode in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"content of {rel}\n".encode())
        path.chmod(mode)


_MODES = {
    "id_key": 0o600,
    "readme.txt": 0o644,
    "bin/run.sh": 0o755,
    "deep/er/private.pem": 0o600,
    "deep/er/notes.txt": 0o640,
    "deep/read_only.cfg": 0o400,
}


async def _delete_dir(ws: Path, name: str) -> mut.TurnJournal:
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        await DeleteDirectory(workspace=ws).execute(path=name, recursive=True)
    finally:
        mut.reset_active_journal(token)
    return journal


@posix_only
async def test_rewinding_a_folder_deletion_restores_every_files_mode(tmp_path, restore_umask):
    os.umask(0o022)  # a plain write would make every file 0644
    root = tmp_path / "secrets"
    _tree(root, _MODES)
    journal = await _delete_dir(tmp_path, "secrets")
    assert not root.exists()

    report = journal.rewind()

    assert report.restored == [str(root)] and report.skipped == []
    for rel, mode in _MODES.items():
        path = root / rel
        assert path.read_bytes() == f"content of {rel}\n".encode()
        got = stat.S_IMODE(path.stat().st_mode)
        assert got == mode, f"{rel}: {oct(got)} != {oct(mode)}"
    assert _no_temp_files(root)


@posix_only
async def test_every_restored_file_is_owner_only_from_the_moment_it_exists(
    tmp_path, restore_umask, monkeypatch
):
    os.umask(0o000)  # the most permissive umask: nothing but the code keeps files private
    root = tmp_path / "secrets"
    _tree(root, {"a.key": 0o600, "b.txt": 0o644, "c.sh": 0o755})
    journal = await _delete_dir(tmp_path, "secrets")

    created: list[int] = []
    real_open = os.open

    def spy(name, flags, mode=0o777, **kw):
        fd = real_open(name, flags, mode, **kw)
        if flags & os.O_CREAT and mut._TMP_PREFIX in str(name):
            created.append(stat.S_IMODE(os.fstat(fd).st_mode))  # at the instant of creation
        return fd

    monkeypatch.setattr(os, "open", spy)
    journal.rewind()
    assert created == [0o600, 0o600, 0o600], [oct(m) for m in created]
    assert stat.S_IMODE((root / "c.sh").stat().st_mode) == 0o755  # widened afterwards


@posix_only
async def test_setuid_setgid_and_sticky_bits_are_never_restored(tmp_path, restore_umask):
    root = tmp_path / "tools"
    _tree(root, {"tool": 0o755})
    (root / "tool").chmod(0o7755)
    journal = await _delete_dir(tmp_path, "tools")
    (entry,) = journal.entries
    assert entry.modes == {"tool": 0o755}
    journal.rewind()
    assert stat.S_IMODE((root / "tool").stat().st_mode) == 0o755


# ── #41: the restore logic, on every platform ────────────────────────────────


def test_the_snapshot_records_each_files_mode_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "a.txt").write_bytes(b"a")
    (tmp_path / "d" / "sub" / "b.txt").write_bytes(b"b")
    snap = mut.capture_tree(tmp_path / "d")
    assert snap is not None
    assert snap.entries == [("sub", None), ("a.txt", b"a"), ("sub/b.txt", b"b")]
    assert set(snap.modes) == {"a.txt", "sub/b.txt"}
    assert all(0 <= m <= 0o777 for m in snap.modes.values())
    assert mut.snapshot_tree(tmp_path / "d") == snap.entries  # the old function is unchanged


def test_no_modes_are_recorded_where_permission_bits_do_not_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", False)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.txt").write_bytes(b"a")
    snap = mut.capture_tree(tmp_path / "d")
    assert snap is not None and snap.modes == {}


def test_each_file_is_restored_atomically_with_its_recorded_mode(tmp_path, monkeypatch):
    calls: list[tuple[str, bytes, int | None, str | None]] = []

    def record(path, data, *, expected_sha256=None, mode=None):
        calls.append((path.relative_to(tmp_path / "d").as_posix(), data, mode, expected_sha256))
        return mut.sha256_bytes(data)

    monkeypatch.setattr(mut, "atomic_write", record)
    snapshot = [("sub", None), ("a.key", b"k"), ("sub/b.sh", b"s")]
    entry = mut.DirDeleted(tmp_path / "d", snapshot, {"a.key": 0o600, "sub/b.sh": 0o755})
    journal = mut.TurnJournal()
    journal.add(entry)
    report = journal.rewind()
    assert calls == [("a.key", b"k", 0o600, mut.ABSENT), ("sub/b.sh", b"s", 0o755, mut.ABSENT)]
    assert (tmp_path / "d" / "sub").is_dir()
    assert report.restored == [str(tmp_path / "d")]


def test_a_legacy_entry_without_modes_still_restores_its_files(tmp_path, monkeypatch):
    """DirDeleted(path, snapshot) as built before modes were kept."""
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", False)
    journal = mut.TurnJournal()
    journal.add(mut.DirDeleted(tmp_path / "d", [("sub", None), ("sub/f.txt", b"data")]))
    report = journal.rewind()
    assert (tmp_path / "d" / "sub" / "f.txt").read_bytes() == b"data"
    assert report.restored == [str(tmp_path / "d")]


def test_a_legacy_entry_says_its_permissions_were_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "_NEW_FILE_FOLLOWS_UMASK", True)
    modes: list = []

    def record(path, data, *, expected_sha256=None, mode=None):
        modes.append(mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return mut.sha256_bytes(data)

    monkeypatch.setattr(mut, "atomic_write", record)
    journal = mut.TurnJournal()
    journal.add(mut.DirDeleted(tmp_path / "d", [("f.txt", b"data")]))
    report = journal.rewind()
    assert modes == [None]  # no mode is invented for an old record
    assert report.restored == [
        f"{tmp_path / 'd'} (its files' permissions were not recorded; they have default "
        "permissions)"
    ]


def test_a_file_that_cannot_be_restored_is_reported_and_the_folder_is_not_called_restored(
    tmp_path, monkeypatch
):
    real = mut.atomic_write

    def flaky(path, data, **kw):
        if path.name == "b.txt":
            raise OSError("disk full")
        return real(path, data, **kw)

    monkeypatch.setattr(mut, "atomic_write", flaky)
    snapshot = [("a.txt", b"a"), ("b.txt", b"b"), ("c.txt", b"c")]
    journal = mut.TurnJournal()
    journal.add(mut.DirDeleted(tmp_path / "d", snapshot, {}))
    report = journal.rewind()

    assert report.restored == []
    (message,) = report.skipped
    assert "only partly restored, 1 of 3 file(s)" in message and "b.txt" in message
    assert (tmp_path / "d" / "a.txt").read_bytes() == b"a"
    assert (tmp_path / "d" / "c.txt").read_bytes() == b"c"


def test_a_failed_atomic_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    real_replace = mut._replace

    def failing(source, target):
        if target.name == "b.txt":
            raise PermissionError("the file is locked")
        real_replace(source, target)

    monkeypatch.setattr(mut, "_replace", failing)
    journal = mut.TurnJournal()
    journal.add(mut.DirDeleted(tmp_path / "d", [("a.txt", b"a"), ("b.txt", b"b")], {}))
    report = journal.rewind()
    assert "only partly restored" in report.skipped[0]
    assert not (tmp_path / "d" / "b.txt").exists()
    assert _no_temp_files(tmp_path)


def test_an_interrupted_restore_is_not_reported_as_done_and_is_not_repeated(tmp_path, monkeypatch):
    real = mut.atomic_write

    def interrupted(path, data, **kw):
        if path.name == "b.txt":
            raise KeyboardInterrupt
        return real(path, data, **kw)

    monkeypatch.setattr(mut, "atomic_write", interrupted)
    journal = mut.TurnJournal()
    journal.add(mut.DirDeleted(tmp_path / "d", [("a.txt", b"a"), ("b.txt", b"b")], {}))
    with pytest.raises(KeyboardInterrupt):
        journal.rewind()
    assert _no_temp_files(tmp_path)
    monkeypatch.setattr(mut, "atomic_write", real)

    # The entry is still there, and a second rewind will not overwrite what is now on disk.
    report = journal.rewind()
    assert report.restored == []
    assert "something exists there now" in report.skipped[0]


def test_error_messages_never_contain_file_content(tmp_path, monkeypatch):
    secret = b"TOKEN-hush-5521"

    def failing(path, data, **kw):
        raise OSError(f"cannot write {path}")

    monkeypatch.setattr(mut, "atomic_write", failing)
    journal = mut.TurnJournal()
    journal.add(mut.DirDeleted(tmp_path / "d", [("t.txt", secret)], {}))
    report = journal.rewind()
    assert secret.decode() not in " ".join(report.skipped)


def test_the_snapshot_limit_still_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "JOURNAL_MAX_BYTES", 1000)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.bin").write_bytes(b"x" * 600)
    (tmp_path / "d" / "b.bin").write_bytes(b"x" * 600)
    meter = ReadMeter(monkeypatch)
    assert mut.capture_tree(tmp_path / "d") is None
    assert meter.total <= 600  # the second file is never read: it would exceed the budget


async def test_delete_directory_journals_modes_alongside_the_snapshot(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("x")
    journal = await _delete_dir(tmp_path, "d")
    (entry,) = journal.entries
    assert entry.snapshot == [("f.txt", b"x")]
    if os.name == "posix":
        assert set(entry.modes) == {"f.txt"}
    else:
        assert entry.modes == {}  # Windows: no POSIX permission bits are recorded or applied
