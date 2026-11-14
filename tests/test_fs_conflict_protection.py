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
import sys
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


# ── #34: move_path checks the destination and the source ─────────────────────


async def test_overwriting_a_destination_changed_since_it_was_read_is_a_conflict(
    tmp_path, journal, monkeypatch
):
    _, read, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_text("source\n", newline="")
    (tmp_path / "dst.txt").write_text("version 1\n", newline="")
    await read.execute(file_path="dst.txt")
    (tmp_path / "dst.txt").write_text("version 2, the user's edit\n", newline="")
    _NothingMoves(monkeypatch)

    with pytest.raises(mut.FileConflictError, match="dst.txt"):
        await move.execute(source="src.txt", destination="dst.txt", overwrite=True)

    assert (tmp_path / "dst.txt").read_text() == "version 2, the user's edit\n"
    assert (tmp_path / "src.txt").read_text() == "source\n"
    assert journal.entries == [] and journal.irreversible == []


async def test_overwriting_an_unchanged_tracked_destination_works_and_rewinds(tmp_path, journal):
    tracker, read, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_text("source\n", newline="")
    (tmp_path / "dst.txt").write_text("destination\n", newline="")
    await read.execute(file_path="dst.txt")

    await move.execute(source="src.txt", destination="dst.txt", overwrite=True)

    assert (tmp_path / "dst.txt").read_text() == "source\n"
    assert not (tmp_path / "src.txt").exists()
    (entry,) = journal.entries
    assert isinstance(entry, mut.Moved) and entry.overwritten == b"destination\n"
    assert tracker.seen(tmp_path / "dst.txt") == _sha(b"source\n")
    assert tracker.seen(tmp_path / "src.txt") is None

    report = journal.rewind()
    assert report.skipped == []
    assert (tmp_path / "src.txt").read_text() == "source\n"
    assert (tmp_path / "dst.txt").read_text() == "destination\n"


async def test_overwriting_a_destination_the_agent_never_read_keeps_working(tmp_path, journal):
    _, _, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_text("source\n", newline="")
    (tmp_path / "dst.txt").write_text("changed by someone, never read by the agent\n", newline="")
    await move.execute(source="src.txt", destination="dst.txt", overwrite=True)
    assert (tmp_path / "dst.txt").read_text() == "source\n"
    (entry,) = journal.entries
    assert entry.overwritten == b"changed by someone, never read by the agent\n"


@pytest.mark.parametrize("overwrite", [False, True], ids=["new-destination", "overwrite"])
async def test_moving_a_source_changed_since_it_was_read_is_a_conflict(
    tmp_path, journal, monkeypatch, overwrite
):
    _, read, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_text("what the agent read\n", newline="")
    if overwrite:
        (tmp_path / "dst.txt").write_text("destination\n", newline="")
    await read.execute(file_path="src.txt")
    (tmp_path / "src.txt").write_text("changed afterwards\n", newline="")
    _NothingMoves(monkeypatch)

    with pytest.raises(mut.FileConflictError, match="src.txt"):
        await move.execute(source="src.txt", destination="dst.txt", overwrite=overwrite)

    assert (tmp_path / "src.txt").read_text() == "changed afterwards\n"
    assert (tmp_path / "dst.txt").exists() is overwrite
    assert journal.entries == []


async def test_a_destination_changed_after_the_checks_is_not_overwritten(
    tmp_path, journal, monkeypatch
):
    """The destination is compared again right before the swap, as atomic_write does."""
    _, _, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_text("source\n", newline="")
    (tmp_path / "dst.txt").write_text("destination\n", newline="")
    real_file_mode = mut.file_mode

    def then_changed(path):  # runs after the version checks, before the swap
        mode = real_file_mode(path)
        (tmp_path / "dst.txt").write_text("written in the gap\n", newline="")
        return mode

    monkeypatch.setattr(mut, "file_mode", then_changed)
    with pytest.raises(mut.FileConflictError, match="dst.txt"):
        await move.execute(source="src.txt", destination="dst.txt", overwrite=True)

    assert (tmp_path / "dst.txt").read_text() == "written in the gap\n"
    assert (tmp_path / "src.txt").read_text() == "source\n"
    assert journal.entries == [] and journal.irreversible == []


async def test_an_irreversible_overwrite_is_recorded_only_once_it_happens(
    tmp_path, journal, monkeypatch
):
    monkeypatch.setattr(mut, "JOURNAL_MAX_BYTES", 10)
    tracker, read, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_text("source\n", newline="")
    (tmp_path / "dst.txt").write_text("a destination too big for the journal\n", newline="")
    await read.execute(file_path="dst.txt")
    (tmp_path / "dst.txt").write_text("a different destination, still too big\n", newline="")

    with pytest.raises(mut.FileConflictError):
        await move.execute(source="src.txt", destination="dst.txt", overwrite=True)
    assert journal.irreversible == []  # nothing was overwritten, so nothing is claimed

    tracker.forget(tmp_path / "dst.txt")
    await move.execute(source="src.txt", destination="dst.txt", overwrite=True)
    assert journal.irreversible == [f"overwrote {tmp_path / 'dst.txt'}"]


async def test_overwrite_without_the_flag_and_folder_rules_are_unchanged(tmp_path, journal):
    _, _, move, _ = _tools(tmp_path)
    (tmp_path / "a.txt").write_text("a\n", newline="")
    (tmp_path / "b.txt").write_text("b\n", newline="")
    (tmp_path / "folder").mkdir()
    with pytest.raises(FileExistsError, match="overwrite=true"):
        await move.execute(source="a.txt", destination="b.txt")
    with pytest.raises(FileExistsError, match="only a file can replace a file"):
        await move.execute(source="a.txt", destination="folder", overwrite=True)
    with pytest.raises(ValueError, match="the same"):
        await move.execute(source="a.txt", destination="a.txt")
    assert (tmp_path / "b.txt").read_text() == "b\n" and journal.entries == []


@pytest.mark.parametrize("overwrite", [False, True], ids=["new-destination", "overwrite"])
@pytest.mark.parametrize("tracked", [True, False], ids=["tracked", "untracked"])
async def test_a_source_changed_after_the_first_check_is_not_moved(
    tmp_path, journal, monkeypatch, overwrite, tracked
):
    """The source is checked once more right before the swap: a change made after the first
    check (while the destination's before-image is read and checked) is refused, and neither
    path is touched."""
    import velune.tools.filesystem.dirs as dirs_mod

    _, read, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_bytes(b"what was checked\n")
    if overwrite:
        (tmp_path / "dst.txt").write_bytes(b"destination\n")
    if tracked:
        await read.execute(file_path="src.txt")
    real_ensure_parent = mut.ensure_parent

    def then_source_changes(path):  # runs after the first checks, before the final ones
        (tmp_path / "src.txt").write_bytes(b"changed in the gap\n")
        return real_ensure_parent(path)

    monkeypatch.setattr(mut, "ensure_parent", then_source_changes)
    monkeypatch.setattr(dirs_mod.os, "replace", lambda *a: pytest.fail("the source was moved"))

    with pytest.raises(mut.FileConflictError, match="src.txt"):
        await move.execute(source="src.txt", destination="dst.txt", overwrite=overwrite)

    assert (tmp_path / "src.txt").read_bytes() == b"changed in the gap\n"
    if overwrite:
        assert (tmp_path / "dst.txt").read_bytes() == b"destination\n"
    else:
        assert not (tmp_path / "dst.txt").exists()
    assert journal.entries == [] and journal.irreversible == []


def _across_filesystems(monkeypatch) -> None:
    """Make the in-place rename fail as it does between two filesystems (EXDEV)."""
    import errno

    import velune.tools.filesystem.dirs as dirs_mod

    def cross_device(source, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link", str(source), None, str(target))

    monkeypatch.setattr(dirs_mod.os, "replace", cross_device)


async def test_a_move_across_filesystems_copies_checks_and_then_removes(
    tmp_path, journal, monkeypatch
):
    tracker, _, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_bytes(b"content\n")
    _across_filesystems(monkeypatch)

    await move.execute(source="src.txt", destination="sub/dst.txt")

    assert (tmp_path / "sub" / "dst.txt").read_bytes() == b"content\n"
    assert not (tmp_path / "src.txt").exists()
    assert tracker.seen(tmp_path / "sub" / "dst.txt") == _sha(b"content\n")
    (moved,) = [entry for entry in journal.entries if isinstance(entry, mut.Moved)]
    assert moved.destination == tmp_path / "sub" / "dst.txt"  # "sub/" is journaled too
    journal.rewind()
    assert (tmp_path / "src.txt").read_bytes() == b"content\n"
    assert not (tmp_path / "sub").exists()


async def test_a_source_changed_during_a_cross_filesystem_copy_is_kept(
    tmp_path, journal, monkeypatch
):
    """Not atomic across filesystems, but a source that changed while it was copied is
    never deleted: the copy is removed instead and the move refused."""
    import velune.tools.filesystem.dirs as dirs_mod

    _, _, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_bytes(b"content\n")
    _across_filesystems(monkeypatch)
    real_copy = dirs_mod.shutil.copyfileobj

    def then_source_changes(reader, writer, *args):
        real_copy(reader, writer, *args)
        (tmp_path / "src.txt").write_bytes(b"written during the copy\n")

    monkeypatch.setattr(dirs_mod.shutil, "copyfileobj", then_source_changes)
    with pytest.raises(mut.FileConflictError, match="src.txt"):
        await move.execute(source="src.txt", destination="dst.txt")

    assert (tmp_path / "src.txt").read_bytes() == b"written during the copy\n"
    assert not (tmp_path / "dst.txt").exists()
    assert journal.entries == []


async def test_a_cross_filesystem_copy_never_replaces_a_file_that_appeared(
    tmp_path, journal, monkeypatch
):
    import velune.tools.filesystem.dirs as dirs_mod

    _, _, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_bytes(b"content\n")
    _across_filesystems(monkeypatch)
    real_open = open

    def someone_creates_it_first(file, mode="r", *args, **kwargs):
        if "x" in mode:
            Path(file).write_bytes(b"someone else's file\n")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(dirs_mod, "open", someone_creates_it_first, raising=False)
    with pytest.raises(FileExistsError):
        await move.execute(source="src.txt", destination="dst.txt")

    assert (tmp_path / "dst.txt").read_bytes() == b"someone else's file\n"
    assert (tmp_path / "src.txt").read_bytes() == b"content\n"
    assert journal.entries == []


# ── #37: case-only renames, and rewinding them ───────────────────────────────


async def test_case_only_file_rename_is_applied_and_rewound(insensitive, journal):
    tracker, read, move, _ = _tools(insensitive)
    (insensitive / "Readme.md").write_text("docs\n", newline="")
    await read.execute(file_path="Readme.md")

    out = await move.execute(source="Readme.md", destination="README.md")

    assert "case only" in out
    assert _listing(insensitive) == ["README.md"]
    assert (insensitive / "README.md").read_text() == "docs\n"
    assert tracker.seen(insensitive / "README.md") == _sha(b"docs\n")
    (entry,) = journal.entries
    assert entry.source.name == "Readme.md" and entry.destination.name == "README.md"

    report = journal.rewind()

    assert report.skipped == [] and len(report.restored) == 1
    assert _listing(insensitive) == ["Readme.md"]
    assert (insensitive / "Readme.md").read_text() == "docs\n"
    assert _no_temporaries(insensitive)


async def test_case_only_folder_rename_is_applied_and_rewound(insensitive, journal):
    _, _, move, _ = _tools(insensitive)
    (insensitive / "docs").mkdir()
    (insensitive / "docs" / "a.txt").write_text("a\n", newline="")

    await move.execute(source="docs", destination="Docs")

    assert _listing(insensitive) == ["Docs"]
    assert (insensitive / "Docs" / "a.txt").read_text() == "a\n"
    report = journal.rewind()
    assert report.skipped == []
    assert _listing(insensitive) == ["docs"]
    assert (insensitive / "docs" / "a.txt").read_text() == "a\n"


async def test_rewind_restores_the_spelling_on_disk_not_the_one_typed(insensitive, journal):
    """The agent may type the source in another case; the original spelling is what returns."""
    _, _, move, _ = _tools(insensitive)
    (insensitive / "Config.json").write_text("{}\n", newline="")
    await move.execute(source="config.json", destination="CONFIG.json")
    assert _listing(insensitive) == ["CONFIG.json"]
    journal.rewind()
    assert _listing(insensitive) == ["Config.json"]


async def test_a_file_changed_after_a_case_only_rename_is_not_rewound(insensitive, journal):
    _, _, move, _ = _tools(insensitive)
    (insensitive / "Readme.md").write_text("docs\n", newline="")
    await move.execute(source="Readme.md", destination="README.md")
    (insensitive / "README.md").write_text("edited by the user\n", newline="")

    report = journal.rewind()

    assert report.restored == []
    assert report.skipped == [
        f"{insensitive / 'README.md'}: changed since this turn, left as it is"
    ]
    assert _listing(insensitive) == ["README.md"]
    assert (insensitive / "README.md").read_text() == "edited by the user\n"


async def test_a_case_only_rename_checks_the_source_version(insensitive, journal, monkeypatch):
    _, read, move, _ = _tools(insensitive)
    (insensitive / "Readme.md").write_text("docs\n", newline="")
    await read.execute(file_path="Readme.md")
    (insensitive / "Readme.md").write_text("changed by the user\n", newline="")
    _NothingMoves(monkeypatch)
    with pytest.raises(mut.FileConflictError):
        await move.execute(source="Readme.md", destination="README.md")
    assert _listing(insensitive) == ["Readme.md"] and journal.entries == []


async def test_a_failed_case_only_rename_puts_the_file_back(insensitive, journal, monkeypatch):
    _, _, move, _ = _tools(insensitive)
    (insensitive / "Readme.md").write_text("docs\n", newline="")
    real_rename = mut._rename_no_replace
    calls = []

    def second_step_fails(a, b):
        calls.append((Path(a).name, Path(b).name))
        if len(calls) == 2:
            raise PermissionError("locked by another program")
        real_rename(a, b)

    monkeypatch.setattr(mut, "_rename_no_replace", second_step_fails)
    with pytest.raises(PermissionError, match="locked"):
        await move.execute(source="Readme.md", destination="README.md")
    assert _listing(insensitive) == ["Readme.md"]
    assert (insensitive / "Readme.md").read_text() == "docs\n"
    assert _no_temporaries(insensitive) and journal.entries == []


async def test_case_only_rename_on_a_case_sensitive_filesystem_is_an_ordinary_move(
    sensitive, journal
):
    _, _, move, _ = _tools(sensitive)
    (sensitive / "Readme.md").write_text("docs\n", newline="")
    out = await move.execute(source="Readme.md", destination="README.md")
    assert "case only" not in out
    assert _listing(sensitive) == ["README.md"]
    journal.rewind()
    assert _listing(sensitive) == ["Readme.md"]


async def test_two_files_differing_only_in_case_stay_two_files(sensitive, journal):
    _, _, move, _ = _tools(sensitive)
    (sensitive / "Readme.md").write_text("one\n", newline="")
    (sensitive / "README.md").write_text("two\n", newline="")
    with pytest.raises(FileExistsError):
        await move.execute(source="Readme.md", destination="README.md")
    assert _listing(sensitive) == ["README.md", "Readme.md"]


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
async def test_hard_links_differing_only_in_case_are_not_a_case_only_rename(sensitive):
    (sensitive / "Readme.md").write_text("one inode\n", newline="")
    os.link(sensitive / "Readme.md", sensitive / "README.md")
    assert not mut.is_respelling(sensitive / "Readme.md", sensitive / "README.md")


def test_rewinding_a_respelled_entry_renames_it_back_on_any_filesystem(tmp_path, monkeypatch):
    """The undo decision with the case-insensitive same-entry check injected (#37).

    The state after a case-only rename is built directly: only ``README.md`` exists. The
    real check would say the two spellings are one entry only on a case-insensitive
    filesystem; here it is injected, so the decision runs on Linux too.
    """
    (tmp_path / "README.md").write_text("docs\n", newline="")
    sha = mut.file_sha256(tmp_path / "README.md")
    journal = mut.TurnJournal()
    journal.add(mut.Moved(tmp_path / "Readme.md", tmp_path / "README.md", None, sha))
    monkeypatch.setattr(
        mut, "is_respelling", lambda current, wanted: current.name.lower() == wanted.name.lower()
    )
    if not _case_insensitive(tmp_path):
        # Without the injection the old spelling is absent here and the ordinary undo runs;
        # with it, the respelling branch must run instead. Prove it took that branch.
        monkeypatch.setattr(mut.shutil, "move", lambda *a: pytest.fail("ordinary undo ran"))

    report = journal.rewind()

    assert report.skipped == []
    assert _listing(tmp_path) == ["Readme.md"]
    assert (tmp_path / "Readme.md").read_text() == "docs\n"


def test_the_respelling_check_on_a_real_filesystem(tmp_path):
    (tmp_path / "Readme.md").write_text("docs\n", newline="")
    insensitive = _case_insensitive(tmp_path)
    assert mut.is_respelling(tmp_path / "Readme.md", tmp_path / "README.md") is insensitive
    assert not mut.is_respelling(tmp_path / "Readme.md", tmp_path / "Readme.md")
    assert not mut.is_respelling(tmp_path / "Readme.md", tmp_path / "Other.md")
    (tmp_path / "sub").mkdir()
    assert not mut.is_respelling(tmp_path / "Readme.md", tmp_path / "sub" / "README.md")


def test_a_link_named_like_its_target_is_not_respelled(tmp_path):
    """A destination given as a link that resolves to the source is not a case-only rename."""
    from velune.tools.filesystem.dirs import _case_only_rename

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data").write_text("x\n", newline="")
    try:
        os.symlink(tmp_path / "sub" / "data", tmp_path / "Data")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    src = tmp_path / "sub" / "data"
    assert _case_only_rename(src, src, "Data", tmp_path) is None


async def test_a_case_only_rename_is_shown_for_approval_with_the_spelling_asked_for(insensitive):
    """Resolving rewrites the name to its stored spelling on Windows; the approval prompt
    must not read "Readme.md -> Readme.md"."""
    from velune.permissions.boundary import Boundary

    (insensitive / "Readme.md").write_text("docs\n", newline="")
    actions = MovePath(workspace=insensitive).describe_actions(
        {"source": "Readme.md", "destination": "README.md"}, Boundary(insensitive)
    )
    assert Path(actions[0].detail).name == "README.md"


async def test_a_file_created_mid_rename_is_never_replaced(insensitive, journal, monkeypatch):
    """Another program creates the new spelling between the two rename steps: nothing is
    renamed onto it, and the error says where the original now is."""
    _, _, move, _ = _tools(insensitive)
    (insensitive / "Readme.md").write_text("docs\n", newline="")
    real_rename = mut._rename_no_replace  # the real no-replace operation runs every step

    def then_someone_writes(a, b):
        real_rename(a, b)
        if Path(b).name.startswith(mut._TMP_PREFIX):
            (insensitive / "README.md").write_text("someone else's file\n", newline="")

    monkeypatch.setattr(mut, "_rename_no_replace", then_someone_writes)
    with pytest.raises(mut.MutationVerificationError, match="nothing was replaced") as caught:
        await move.execute(source="Readme.md", destination="README.md")

    assert (insensitive / "README.md").read_text() == "someone else's file\n"
    (parked,) = [n for n in os.listdir(insensitive) if n.startswith(mut._TMP_PREFIX)]
    assert parked in str(caught.value)
    assert (insensitive / parked).read_text() == "docs\n"
    assert journal.entries == []


@pytest.mark.parametrize("kind", ["file", "empty-folder", "folder"])
def test_the_no_replace_rename_never_replaces_an_existing_entry(tmp_path, kind):
    """POSIX rename would silently replace a file or an empty folder; this must not."""
    source, target = tmp_path / "source", tmp_path / "target"
    if kind == "file":
        source.write_bytes(b"moving\n")
        target.write_bytes(b"already there\n")
    else:
        source.mkdir()
        (source / "inside.txt").write_bytes(b"moving\n")
        target.mkdir()
        if kind == "folder":
            (target / "kept.txt").write_bytes(b"already there\n")
    with pytest.raises(OSError):
        mut._rename_no_replace(source, target)
    if kind == "file":
        assert source.read_bytes() == b"moving\n"
        assert target.read_bytes() == b"already there\n"
    else:
        assert (source / "inside.txt").read_bytes() == b"moving\n"
        assert _listing(target) == ([] if kind == "empty-folder" else ["kept.txt"])


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_the_no_replace_rename_moves_to_a_free_name(tmp_path, kind):
    source, target = tmp_path / "source", tmp_path / "target"
    if kind == "file":
        source.write_bytes(b"data\n")
    else:
        source.mkdir()
        (source / "inside.txt").write_bytes(b"data\n")
    mut._rename_no_replace(source, target)
    assert not os.path.lexists(source)
    assert (target if kind == "file" else target / "inside.txt").read_bytes() == b"data\n"


def test_a_respelling_that_cannot_finish_does_not_abandon_the_rest_of_the_rewind(
    tmp_path, monkeypatch
):
    (tmp_path / "README.md").write_bytes(b"docs\n")
    (tmp_path / "created.txt").write_bytes(b"made this turn\n")
    journal = mut.TurnJournal()
    journal.add(
        mut.FileChange(tmp_path / "created.txt", None, mut.file_sha256(tmp_path / "created.txt"))
    )
    journal.add(
        mut.Moved(
            tmp_path / "Readme.md",
            tmp_path / "README.md",
            None,
            mut.file_sha256(tmp_path / "README.md"),
        )
    )
    monkeypatch.setattr(mut, "is_respelling", lambda current, wanted: True)

    def stuck(current, wanted):
        raise mut.MutationVerificationError(f"{current.name} is now at .velune-tmp-x.rename")

    monkeypatch.setattr(mut, "respell", stuck)
    report = journal.rewind()  # newest first: the respelling fails, the creation is undone

    assert not (tmp_path / "created.txt").exists()
    assert report.restored == [str(tmp_path / "created.txt")]
    (skipped,) = report.skipped
    assert "README.md" in skipped and ".velune-tmp-x.rename" in skipped


async def test_a_replaced_destination_is_journaled_even_if_verification_then_fails(
    tmp_path, journal, monkeypatch
):
    """Another program writes to the destination right after the swap: the check fails, but
    the destination that was replaced is still in the journal."""
    import velune.tools.filesystem.dirs as dirs_mod

    _, _, move, _ = _tools(tmp_path)
    (tmp_path / "src.txt").write_bytes(b"source\n")
    (tmp_path / "dst.txt").write_bytes(b"the destination it replaced\n")
    real_replace = os.replace

    def then_someone_writes(a, b):
        real_replace(a, b)
        with open(b, "ab") as handle:
            handle.write(b"appended right after the swap\n")

    monkeypatch.setattr(dirs_mod.os, "replace", then_someone_writes)
    with pytest.raises(mut.MutationVerificationError, match="content"):
        await move.execute(source="src.txt", destination="dst.txt", overwrite=True)

    (entry,) = journal.entries
    assert isinstance(entry, mut.Moved)
    assert entry.overwritten == b"the destination it replaced\n"


def _folder_with_content(path: Path) -> Path:
    path.mkdir()
    (path / "inside.txt").write_bytes(b"the folder's content\n")
    return path


def test_an_empty_folder_appearing_at_the_last_moment_is_not_replaced(tmp_path, monkeypatch):
    """Another program creates an empty folder at the target just before the rename. POSIX
    rename would replace an empty folder silently; the rename must refuse instead, keep
    that folder, and leave the source where it was."""
    source = _folder_with_content(tmp_path / "source")
    target = tmp_path / "target"
    real_exclusive = mut._rename_exclusive

    def competitor_first(a, b):
        os.mkdir(b)  # the competing empty folder, created at the last possible moment
        return real_exclusive(a, b)

    monkeypatch.setattr(mut, "_rename_exclusive", competitor_first)
    with pytest.raises(FileExistsError):
        mut._rename_no_replace(source, target)

    assert target.is_dir() and os.listdir(target) == []  # the competitor's folder is kept
    assert (source / "inside.txt").read_bytes() == b"the folder's content\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX: Windows rename never replaces anyway")
def test_a_folder_rename_uses_no_placeholder_on_posix(tmp_path, monkeypatch):
    """No mkdir placeholder and no plain rename: either could be raced into replacing an
    empty folder that someone else created (the defect this guards against)."""
    source = _folder_with_content(tmp_path / "source")
    monkeypatch.setattr(mut.os, "mkdir", lambda *a, **k: pytest.fail("placeholder created"))
    monkeypatch.setattr(mut.os, "rename", lambda *a, **k: pytest.fail("plain rename used"))
    try:
        mut._rename_no_replace(source, tmp_path / "target")
    except mut.NoReplaceUnsupportedError:
        assert source.is_dir()  # fails closed where there is no atomic primitive
        return
    assert (tmp_path / "target" / "inside.txt").read_bytes() == b"the folder's content\n"


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="Linux renameat2 / macOS renamex_np",
)
def test_the_kernel_no_replace_rename_is_available_on_linux_and_macos(tmp_path):
    """The Linux and macOS CI jobs exercise the atomic kernel call, not the fallback."""
    assert mut._native_rename() is not None
    source = _folder_with_content(tmp_path / "source")
    (tmp_path / "taken").mkdir()
    with pytest.raises(FileExistsError):
        mut._rename_exclusive(source, tmp_path / "taken")
    mut._rename_exclusive(source, tmp_path / "free")
    assert (tmp_path / "free" / "inside.txt").exists() and not source.exists()


def test_a_folder_fails_closed_without_an_atomic_rename(tmp_path, monkeypatch):
    source = _folder_with_content(tmp_path / "source")

    def unsupported(a, b):
        raise mut.NoReplaceUnsupportedError(38, "not supported here", str(a))

    monkeypatch.setattr(mut, "_rename_exclusive", unsupported)
    with pytest.raises(mut.NoReplaceUnsupportedError):
        mut._rename_no_replace(source, tmp_path / "target")
    assert (source / "inside.txt").exists() and not (tmp_path / "target").exists()


def test_a_file_without_an_atomic_rename_falls_back_to_a_hard_link(tmp_path, monkeypatch):
    (tmp_path / "source").write_bytes(b"data\n")
    (tmp_path / "taken").write_bytes(b"someone else's\n")

    def unsupported(a, b):
        raise mut.NoReplaceUnsupportedError(38, "not supported here", str(a))

    monkeypatch.setattr(mut, "_rename_exclusive", unsupported)
    with pytest.raises(FileExistsError):
        mut._rename_no_replace(tmp_path / "source", tmp_path / "taken")
    assert (tmp_path / "taken").read_bytes() == b"someone else's\n"
    mut._rename_no_replace(tmp_path / "source", tmp_path / "free")
    assert (tmp_path / "free").read_bytes() == b"data\n" and not (tmp_path / "source").exists()


async def test_a_case_only_folder_rename_keeps_a_competing_folder(
    insensitive, journal, monkeypatch
):
    """End to end on a real case-insensitive filesystem: an empty folder created under the
    new spelling between the two rename steps is kept, and the original is not lost."""
    _, _, move, _ = _tools(insensitive)
    _folder_with_content(insensitive / "docs")
    real_exclusive = mut._rename_exclusive

    def competitor_after_first_step(a, b):
        real_exclusive(a, b)
        if Path(b).name.startswith(mut._TMP_PREFIX):
            os.mkdir(insensitive / "Docs")

    monkeypatch.setattr(mut, "_rename_exclusive", competitor_after_first_step)
    with pytest.raises(mut.MutationVerificationError, match="nothing was replaced") as caught:
        await move.execute(source="docs", destination="Docs")

    assert os.listdir(insensitive / "Docs") == []  # the competitor's empty folder
    (parked,) = [n for n in os.listdir(insensitive) if n.startswith(mut._TMP_PREFIX)]
    assert parked in str(caught.value)
    assert (insensitive / parked / "inside.txt").read_bytes() == b"the folder's content\n"
    assert journal.entries == []


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
