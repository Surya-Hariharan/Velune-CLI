"""Performance tests for the incremental repository indexer.

Scenarios
---------
1. First pass  — 100 fresh Python files: must complete in < 10 s.
2. Second pass — no changes, same git HEAD: empty delta in < 0.5 s.
3. Third pass  — 5 files modified and committed: delta contains exactly 5 paths.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_python_file(path: Path, index: int) -> None:
    """Write a synthetic Python file with a unique function."""
    path.write_text(
        f"# generated file {index}\n"
        f"def func_{index}(x):\n"
        f"    return x * {index}\n"
        f"\nclass Class_{index}:\n"
        f"    value = {index}\n",
        encoding="utf-8",
    )


def _git(workspace: Path, *args: str) -> None:
    """Run a git command in *workspace*, raising on failure."""
    subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        check=True,
        capture_output=True,
    )


def _setup_git_repo(workspace: Path) -> None:
    """Initialise a git repo with a single root commit."""
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@velune.dev")
    _git(workspace, "config", "user.name", "Velune Test")


def _commit_all(workspace: Path, message: str) -> str:
    """Stage all changes and commit; return the new HEAD SHA."""
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", message)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _run(coro) -> object:
    """Run an async coroutine from synchronous test code."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with 100 Python files and an initial git commit."""
    ws = tmp_path / "repo"
    ws.mkdir()
    _setup_git_repo(ws)

    src = ws / "src"
    src.mkdir()
    for i in range(100):
        _make_python_file(src / f"module_{i:03d}.py", i)

    _commit_all(ws, "initial: 100 modules")
    return ws


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_pass_completes_within_10_seconds(workspace: Path) -> None:
    """First indexing of 100 files must finish in under 10 seconds."""
    from velune.repository.incremental_indexer import IncrementalIndexer

    state_path = workspace / ".velune" / "index_state.json"
    inc = IncrementalIndexer(workspace, state_path)

    t0 = time.perf_counter()
    delta = _run(inc.compute_delta())
    _run(inc.apply_delta(delta))
    elapsed = time.perf_counter() - t0

    # All 100 files are new on first pass
    assert len(delta.to_add) == 100
    assert len(delta.to_update) == 0
    assert len(delta.to_remove) == 0
    assert elapsed < 10.0, f"First pass took {elapsed:.2f}s — expected < 10s"


def test_second_pass_empty_delta_under_half_second(workspace: Path) -> None:
    """Second pass with no changes must return an empty delta in < 0.5 s."""
    from velune.repository.incremental_indexer import IncrementalIndexer

    state_path = workspace / ".velune" / "index_state.json"
    inc = IncrementalIndexer(workspace, state_path)

    # First pass — prime the index
    delta1 = _run(inc.compute_delta())
    _run(inc.apply_delta(delta1))

    # Second pass — nothing changed; git SHA matches, working tree is clean
    t0 = time.perf_counter()
    delta2 = _run(inc.compute_delta())
    elapsed = time.perf_counter() - t0

    assert delta2.is_empty, f"Expected empty delta; got {delta2}"
    assert elapsed < 0.5, f"Second pass took {elapsed:.3f}s — expected < 0.5s"


def test_third_pass_detects_exactly_five_changed_files(workspace: Path) -> None:
    """After modifying and committing 5 files, delta must contain exactly those 5."""
    from velune.repository.incremental_indexer import IncrementalIndexer

    state_path = workspace / ".velune" / "index_state.json"
    inc = IncrementalIndexer(workspace, state_path)

    # First pass — prime the index
    delta1 = _run(inc.compute_delta())
    _run(inc.apply_delta(delta1))

    # Modify 5 files
    src = workspace / "src"
    modified: list[str] = []
    for i in range(5):
        path = src / f"module_{i:03d}.py"
        path.write_text(
            f"# MODIFIED file {i}\ndef updated_func_{i}(): return 'changed'\n",
            encoding="utf-8",
        )
        modified.append(f"src/module_{i:03d}.py")

    # Commit the changes so git HEAD moves (required for the SHA fast path to re-scan)
    _commit_all(workspace, "modify 5 modules")

    # Third pass
    delta3 = _run(inc.compute_delta())

    assert not delta3.is_empty, "Expected non-empty delta after modifications"
    changed = set(delta3.to_update)
    for rel in modified:
        assert rel in changed, f"{rel} not found in delta.to_update ({changed})"
    assert len(delta3.to_add) == 0
    assert len(delta3.to_remove) == 0
    assert len(delta3.to_update) == 5, (
        f"Expected 5 updated files, got {len(delta3.to_update)}: {delta3.to_update}"
    )
