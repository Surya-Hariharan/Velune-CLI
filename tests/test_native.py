"""Tests for velune/repository/_native.py

Verifies that the pure-Python fallbacks behave correctly whether or not the
Rust extension is available.  Tests run against the Python implementations
directly so they are always exercised.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from velune.repository import _native


# ─── sha256_file ─────────────────────────────────────────────────────────────


class TestSha256File:
    def test_known_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        # SHA-256 of empty input
        assert _native.sha256_file(f) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_known_content(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_bytes(b"hello world")
        digest = _native.sha256_file(f)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"velune" * 10000)
        d1 = _native.sha256_file(f)
        d2 = _native.sha256_file(str(f))
        assert d1 == d2

    def test_different_contents_differ(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"aaa")
        b.write_bytes(b"bbb")
        assert _native.sha256_file(a) != _native.sha256_file(b)

    def test_accepts_pathlib(self, tmp_path: Path) -> None:
        f = tmp_path / "p.txt"
        f.write_text("test", encoding="utf-8")
        result = _native.sha256_file(f)
        assert isinstance(result, str) and len(result) == 64

    def test_missing_file_raises(self) -> None:
        with pytest.raises(OSError):
            _native.sha256_file("/nonexistent/path/to/file.txt")

    def test_python_fallback_matches_hashlib(self, tmp_path: Path) -> None:
        import hashlib

        f = tmp_path / "ref.bin"
        f.write_bytes(b"reference content")
        expected = hashlib.sha256(b"reference content").hexdigest()
        assert _native._sha256_file_py(str(f)) == expected


# ─── scan_directory ───────────────────────────────────────────────────────────


class TestScanDirectory:
    def _make_tree(self, root: Path) -> None:
        (root / "main.py").write_text("# main")
        (root / "utils.py").write_text("# utils")
        (root / "README.md").write_text("# docs")
        sub = root / "sub"
        sub.mkdir()
        (sub / "helper.py").write_text("# helper")
        (sub / "data.json").write_text("{}")
        skip = root / ".venv"
        skip.mkdir()
        (skip / "site.py").write_text("# venv")
        node = root / "node_modules"
        node.mkdir()
        (node / "lib.js").write_text("// lib")

    def test_filters_by_extension(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        found = _native.scan_directory(tmp_path, [".py"], [])
        names = {Path(p).name for p in found}
        assert names == {"main.py", "utils.py", "helper.py", "site.py"}

    def test_skips_directories(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        found = _native.scan_directory(tmp_path, [".py"], [".venv", "node_modules"])
        names = {Path(p).name for p in found}
        assert names == {"main.py", "utils.py", "helper.py"}

    def test_empty_extensions_returns_all(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        found = _native.scan_directory(tmp_path, [], [".venv", "node_modules"])
        assert len(found) >= 5  # main.py, utils.py, README.md, helper.py, data.json

    def test_returns_sorted(self, tmp_path: Path) -> None:
        (tmp_path / "b.py").write_text("")
        (tmp_path / "a.py").write_text("")
        found = _native.scan_directory(tmp_path, [".py"], [])
        assert found == sorted(found)

    def test_accepts_pathlib_root(self, tmp_path: Path) -> None:
        (tmp_path / "x.py").write_text("")
        found = _native.scan_directory(tmp_path, [".py"], [])
        assert len(found) == 1

    def test_empty_directory(self, tmp_path: Path) -> None:
        found = _native.scan_directory(tmp_path, [".py"], [])
        assert found == []

    def test_python_fallback_matches_api(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        via_api = _native.scan_directory(tmp_path, [".py"], [".venv"])
        via_py = _native._scan_directory_py(str(tmp_path), [".py"], [".venv"])
        assert sorted(via_api) == sorted(via_py)


# ─── NATIVE_AVAILABLE flag ────────────────────────────────────────────────────


def test_native_available_is_bool() -> None:
    assert isinstance(_native.NATIVE_AVAILABLE, bool)
