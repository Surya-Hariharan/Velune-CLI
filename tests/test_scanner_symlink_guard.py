"""Regression test: FilesystemScanner must not infinite-loop on a symlink cycle.

Windows requires elevated privileges to create real symlinks (WinError 1314
without Developer Mode / admin), so the cycle is simulated with mocked Path
objects whose .resolve() reproduces what a real symlink loop looks like:
two different Path objects resolving to the same real directory.
"""

from __future__ import annotations

from pathlib import Path

from velune.repository.scanner import FilesystemScanner


class _FakeEntry:
    """Minimal Path stand-in — real symlinks aren't creatable without
    elevated privileges on Windows, so a symlink cycle is simulated by two
    directory entries whose .resolve() both return the same real path."""

    _counter = 0

    def __init__(self, *, real_path: Path, is_dir: bool, suffix: str = "", children=None):
        self._real_path = real_path
        self._is_dir = is_dir
        self.suffix = suffix
        self._children = children or []
        _FakeEntry._counter += 1
        self._order = _FakeEntry._counter  # gives sorted() a total order

    def is_dir(self) -> bool:
        return self._is_dir

    def is_file(self) -> bool:
        return not self._is_dir

    def resolve(self) -> Path:
        return self._real_path

    def iterdir(self):
        return iter(self._children)

    def __lt__(self, other: _FakeEntry) -> bool:
        return self._order < other._order


def _make_dir(real_path: Path, children: list) -> _FakeEntry:
    return _FakeEntry(real_path=real_path, is_dir=True, children=children)


def _make_file(suffix: str) -> _FakeEntry:
    return _FakeEntry(real_path=Path("/unused"), is_dir=False, suffix=suffix)


def test_recursive_scan_terminates_on_a_symlink_cycle():
    root = Path("/fake/root").resolve()
    sub_real = root / "sub"

    loop_back_to_root = _make_dir(root, [])  # symlink resolving back to root: the cycle
    a_file = _make_file(".py")
    sub_dir = _make_dir(sub_real, [loop_back_to_root, a_file])
    root_dir = _make_dir(root, [sub_dir])

    scanner = FilesystemScanner.__new__(FilesystemScanner)
    scanner.root_path = root
    scanner.is_ignored = lambda p: False

    accumulator: list[Path] = []
    visited = {root}
    scanner._recursive_scan(root_dir, None, accumulator, visited)

    # Terminated (didn't hang) and only real directories were entered once each.
    assert visited == {root, sub_real}
    assert accumulator == [a_file]


def test_scan_still_finds_all_files_in_an_ordinary_tree_with_no_symlinks(tmp_path):
    """Real-filesystem sanity check that threading visited_dirs through the
    recursion didn't change ordinary (non-cycle) scanning behavior."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("a = 1\n")
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "b.py").write_text("b = 2\n")
    (tmp_path / "readme.md").write_text("# hi\n")

    scanner = FilesystemScanner(tmp_path)
    found = {p.name for p in scanner.scan([".py"])}

    assert found == {"a.py", "b.py"}


def test_recursive_scan_skips_a_directory_reached_via_two_different_paths():
    """Not just cycles — two symlinks pointing at the same real directory
    should not cause it to be walked (and its files counted) twice."""
    root = Path("/fake/root2").resolve()
    shared_real = root / "shared"

    shared_file = _make_file(".py")
    shared_dir_via_a = _make_dir(shared_real, [shared_file])
    shared_dir_via_b = _make_dir(shared_real, [shared_file])
    root_dir = _make_dir(root, [shared_dir_via_a, shared_dir_via_b])

    scanner = FilesystemScanner.__new__(FilesystemScanner)
    scanner.root_path = root
    scanner.is_ignored = lambda p: False

    accumulator: list[Path] = []
    visited = {root}
    scanner._recursive_scan(root_dir, None, accumulator, visited)

    assert accumulator == [shared_file]  # not double-counted
