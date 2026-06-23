"""Tests for the manual (user-triggered) repository cognition entry points.

Startup never indexes (see ``test_workspace_index_guard``). These tests cover
the on-demand surface the ``/cognition`` command drives: ``quick_summary``,
``preview``, and ``run_incremental``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from velune.repository.cognition import RepositoryCognitionService


def _make_project(root: Path) -> Path:
    project = root / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    (project / "main.py").write_text(
        "def hello() -> str:\n    return 'hi'\n", encoding="utf-8"
    )
    (project / "util.py").write_text("X = 1\n", encoding="utf-8")
    return project


def test_quick_summary_returns_dict(tmp_path: Path) -> None:
    service = RepositoryCognitionService(_make_project(tmp_path))
    summary = service.quick_summary()
    assert isinstance(summary, dict)
    assert summary.get("root")


def test_preview_counts_files_and_estimates_tokens(tmp_path: Path) -> None:
    service = RepositoryCognitionService(_make_project(tmp_path))
    preview = asyncio.run(service.preview())
    # At least the two .py files are discovered as code files.
    assert preview["file_count"] >= 2
    assert preview["est_tokens"] >= 0
    assert "total_bytes" in preview


def test_run_incremental_indexes_changed_files(tmp_path: Path) -> None:
    service = RepositoryCognitionService(_make_project(tmp_path))
    delta = asyncio.run(service.run_incremental())
    # First run sees the .py files as additions.
    assert getattr(delta, "total", 0) >= 2
    # State file is written so a second run is a no-op.
    second = asyncio.run(service.run_incremental())
    assert getattr(second, "total", 0) == 0
