"""Tests for the workspace-safety guard that prevents auto-indexing unsafe roots.

Bare ``velune`` defaults its workspace to the current directory. Launched from
the user's home directory (or a drive root) it used to recursively walk and hash
the entire tree before the REPL prompt appeared — an effectively unbounded stall.
The guard must flag those roots so indexing is skipped while the REPL still opens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from velune.repository.cognition import RepositoryCognitionService
from velune.repository.scanner import unsafe_index_root_reason


def test_home_directory_is_flagged_unsafe() -> None:
    reason = unsafe_index_root_reason(Path.home())
    assert reason and "home" in reason


def test_filesystem_root_is_flagged_unsafe() -> None:
    root = Path(Path.cwd().anchor)  # e.g. C:\ or /
    assert unsafe_index_root_reason(root) == "a filesystem root"


def test_ordinary_project_dir_is_safe(tmp_path: Path) -> None:
    project = tmp_path / "my-project"
    project.mkdir()
    assert unsafe_index_root_reason(project) is None


def test_initialize_never_indexes(monkeypatch, tmp_path: Path) -> None:
    """initialize() is inert — cognition is manual, so it must never index.

    This holds even for an ordinary (safe) project root: startup must reach the
    prompt with zero repository processing.
    """
    project = tmp_path / "my-project"
    project.mkdir()
    service = RepositoryCognitionService(project)

    created = False

    class _Boom:
        def __init__(self, *a, **k) -> None:
            nonlocal created
            created = True

    monkeypatch.setattr("velune.repository.incremental_indexer.IncrementalIndexer", _Boom)

    asyncio.run(service.initialize())

    assert created is False
    assert service._bg_index_task is None


def test_manual_entry_points_respect_unsafe_guard() -> None:
    """The manual cognition surface still refuses to index unsafe roots."""
    service = RepositoryCognitionService(Path.home())
    assert service.unsafe_reason() and "home" in service.unsafe_reason()


def test_probe_for_changes_returns_false_for_unsafe_root() -> None:
    service = RepositoryCognitionService(Path.home())
    assert asyncio.run(service.probe_for_changes()) is False
