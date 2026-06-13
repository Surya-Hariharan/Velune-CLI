"""Workspace storage-isolation tests — enforces the 'no cross-project
contamination' rule at the path layer.

Heavy state (cognitive core, vector/semantic stores) is keyed per workspace by
:func:`velune.core.paths.workspace_storage_dir`. Two distinct project roots must
resolve to disjoint storage directories, and the mapping must be stable for a
given root so a project always reopens its own memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.core.paths import (
    cognitive_db_path,
    lancedb_store_path,
    workspace_storage_dir,
)


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point app data root at a throwaway dir so tests never touch real state."""
    monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path / "data"))


def test_distinct_workspaces_get_disjoint_storage(tmp_path: Path) -> None:
    proj_a = tmp_path / "project_a"
    proj_b = tmp_path / "project_b"
    proj_a.mkdir()
    proj_b.mkdir()

    dir_a = workspace_storage_dir(proj_a)
    dir_b = workspace_storage_dir(proj_b)

    assert dir_a != dir_b
    # Neither storage dir may be nested inside the other.
    assert dir_a not in dir_b.parents
    assert dir_b not in dir_a.parents
    # And neither lives inside a project tree (avoids cloud-sync + leakage).
    assert proj_a not in dir_a.parents
    assert proj_b not in dir_b.parents


def test_same_named_folders_at_different_paths_do_not_collide(tmp_path: Path) -> None:
    a = tmp_path / "one" / "myapp"
    b = tmp_path / "two" / "myapp"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    assert workspace_storage_dir(a) != workspace_storage_dir(b)


def test_storage_dir_is_stable_for_a_workspace(tmp_path: Path) -> None:
    proj = tmp_path / "project"
    proj.mkdir()
    assert workspace_storage_dir(proj) == workspace_storage_dir(proj)


def test_db_and_vector_paths_isolated_per_workspace(tmp_path: Path) -> None:
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()

    assert cognitive_db_path(proj_a) != cognitive_db_path(proj_b)
    assert lancedb_store_path(proj_a) != lancedb_store_path(proj_b)
