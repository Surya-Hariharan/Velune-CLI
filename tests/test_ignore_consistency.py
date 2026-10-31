"""``read_directory``, ``find_files`` and ``grep_files`` agree on what is ignored.

``find_files`` and ``read_directory`` honoured only ``.veluneignore`` at the workspace
root, so they listed gitignored paths that ``grep_files`` (which goes through the
repository scanner) hid. All three now share one hierarchical ignore matcher:
built-in defaults, then ``.gitignore`` and ``.veluneignore`` at every directory
level, deeper files overriding shallower ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.tools.filesystem.ignore import load_ignore
from velune.tools.filesystem.read import ReadDirectory
from velune.tools.filesystem.search import FindFiles, GrepFiles


def _write(path: Path, text: str = "needle\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path / "main.py")
    _write(tmp_path / "generated" / "out.py")  # root .gitignore: generated/
    _write(tmp_path / "debug.log")  # root .gitignore: *.log
    _write(tmp_path / ".gitignore", "generated/\n*.log\n")

    _write(tmp_path / "sub" / "keep.log")  # nested negation re-includes it
    _write(tmp_path / "sub" / "local_a.py")  # nested: local_*.py
    _write(tmp_path / "sub" / "deep" / "local_b.py")  # a slash-less pattern applies at depth
    _write(tmp_path / "sub" / "deep" / "ok.py")
    _write(tmp_path / "sub" / ".gitignore", "local_*.py\n!keep.log\n")

    _write(tmp_path / "other" / "private" / "secret_notes.py")  # nested .veluneignore
    _write(tmp_path / "other" / "public.py")
    _write(tmp_path / "other" / ".veluneignore", "private/\n")
    return tmp_path


def _rel(spec_root: Path, names: list[str]) -> set[str]:
    return {Path(n).resolve().relative_to(spec_root.resolve()).as_posix() for n in names}


def _ignored(repo: Path, rel: str) -> bool:
    return load_ignore(repo).is_ignored(repo / rel)


@pytest.mark.parametrize(
    "rel",
    [
        "generated/out.py",
        "debug.log",
        "sub/local_a.py",
        "sub/deep/local_b.py",
        "other/private/secret_notes.py",
    ],
)
def test_ignored_paths_are_ignored(repo, rel):
    assert _ignored(repo, rel), rel


@pytest.mark.parametrize(
    "rel", ["main.py", "sub/keep.log", "sub/deep/ok.py", "other/public.py", ".gitignore"]
)
def test_other_paths_are_not_ignored(repo, rel):
    assert not _ignored(repo, rel), rel


def test_a_directory_pattern_ignores_the_directory_itself(repo):
    assert _ignored(repo, "generated")
    assert not _ignored(repo, "sub")


async def test_find_files_honours_nested_gitignore_and_veluneignore(repo):
    found = _rel(repo, [f for f in await FindFiles(workspace=repo).execute("*") if isinstance(f, str)])

    assert {"main.py", "sub/keep.log", "sub/deep/ok.py", "other/public.py"} <= found
    assert not found & {
        "generated/out.py",
        "debug.log",
        "sub/local_a.py",
        "sub/deep/local_b.py",
        "other/private/secret_notes.py",
    }


async def test_read_directory_hides_gitignored_entries(repo):
    assert "generated" not in await ReadDirectory(workspace=repo).execute(".")
    assert "debug.log" not in await ReadDirectory(workspace=repo).execute(".")
    assert "local_a.py" not in await ReadDirectory(workspace=repo).execute("sub")
    assert "keep.log" in await ReadDirectory(workspace=repo).execute("sub")


async def test_grep_files_and_find_files_agree(repo):
    grep = await GrepFiles(workspace=repo).execute("needle")
    grepped = {_rel(repo, [h["file"]]).pop() for h in grep if not h.get("truncated")}
    found = _rel(repo, [f for f in await FindFiles(workspace=repo).execute("*") if isinstance(f, str)])
    text_like = {p for p in found if Path(p).name not in {".gitignore", ".veluneignore"}}

    assert grepped == text_like
