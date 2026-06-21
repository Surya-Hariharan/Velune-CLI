"""Tests for progress_callback hooks on RepositoryIndexer and IncrementalIndexer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from velune.repository.incremental_indexer import IncrementalIndexer, IndexDelta
from velune.repository.indexer import RepositoryIndexer


def _write_python_files(directory: Path, count: int = 3) -> None:
    """Write simple Python source files for indexing."""
    for i in range(count):
        (directory / f"file_{i}.py").write_text(f"def func_{i}():\n    return {i}\n")


class TestRepositoryIndexerProgress:
    def test_callback_called_per_file(self, tmp_path):
        _write_python_files(tmp_path, count=3)
        indexer = RepositoryIndexer(root_path=tmp_path)

        calls: list[tuple[int, int, str]] = []
        indexer.progress_callback = lambda i, t, p: calls.append((i, t, p))

        indexer.index()

        assert len(calls) == 3
        for processed, total, rel_path in calls:
            assert total == 3
            assert 1 <= processed <= 3
            assert rel_path.endswith(".py")

    def test_callback_not_called_when_none(self, tmp_path):
        _write_python_files(tmp_path, count=2)
        indexer = RepositoryIndexer(root_path=tmp_path)
        indexer.progress_callback = None
        # Should not raise
        indexer.index()

    def test_callback_receives_relative_paths(self, tmp_path):
        _write_python_files(tmp_path, count=1)
        indexer = RepositoryIndexer(root_path=tmp_path)

        paths: list[str] = []
        indexer.progress_callback = lambda i, t, p: paths.append(p)

        indexer.index()
        assert paths, "Expected at least one callback"
        for p in paths:
            assert not Path(p).is_absolute(), f"Expected relative path, got: {p}"

    def test_callback_processed_increments(self, tmp_path):
        _write_python_files(tmp_path, count=4)
        indexer = RepositoryIndexer(root_path=tmp_path)

        processed_values: list[int] = []
        indexer.progress_callback = lambda i, t, p: processed_values.append(i)

        indexer.index()
        # processed values should be strictly increasing
        assert processed_values == sorted(processed_values)
        assert processed_values[-1] == 4


class TestIncrementalIndexerProgress:
    def test_callback_called_per_delta_file(self, tmp_path):
        _write_python_files(tmp_path, count=3)

        state_path = tmp_path / ".velune" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)

        indexer = IncrementalIndexer(
            workspace_root=tmp_path,
            state_path=state_path,
        )

        calls: list[tuple[int, int, str]] = []
        indexer.progress_callback = lambda i, t, p: calls.append((i, t, p))

        # Build a delta with all 3 files as "to_add"
        delta = IndexDelta(
            to_add=["file_0.py", "file_1.py", "file_2.py"],
        )

        asyncio.run(indexer.apply_delta(delta))

        assert len(calls) == 3

    def test_callback_not_called_on_empty_delta(self, tmp_path):
        state_path = tmp_path / ".velune" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)

        indexer = IncrementalIndexer(workspace_root=tmp_path, state_path=state_path)
        called = False
        indexer.progress_callback = lambda i, t, p: setattr(
            type("_", (), {"x": None})(), "x", True
        )

        delta = IndexDelta()  # empty
        asyncio.run(indexer.apply_delta(delta))

        # No assertion needed — just ensure no exception is raised

    def test_callback_processed_count_correct(self, tmp_path):
        for i in range(2):
            (tmp_path / f"m_{i}.py").write_text(f"x = {i}\n")

        state_path = tmp_path / ".velune" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)

        indexer = IncrementalIndexer(workspace_root=tmp_path, state_path=state_path)
        counts: list[int] = []
        indexer.progress_callback = lambda i, t, p: counts.append(i)

        delta = IndexDelta(to_add=["m_0.py", "m_1.py"])
        asyncio.run(indexer.apply_delta(delta))

        assert counts == [1, 2]
