"""Tests for git-history evolution signal wiring: co-change clustering,
churn-weighted confidence decay, and rename-lineage metadata.

docs/repository-intelligence-baseline.md's Repository Evolution discussion:
"co-change clusters... often reveal true module boundaries better than
folder structure does"; "churn-weighted decay applied to confidence for
capability claims resting on recently-rewritten code."

- GitTracker.get_co_change_clusters (tracker.py) mines commit history for
  file pairs that repeatedly change together.
- KnowledgeGraphPatcher._churn_confidence (graph_patcher.py) discounts a
  FILE node's confidence based on how often it's changed recently.
- KnowledgeGraphPatcher records rename lineage (from
  IncrementalIndexer._detect_renames) as node metadata rather than a graph
  edge — kg_edges' foreign-key constraint means an edge can't reference
  the old path's node, which is deleted in the same transaction.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

from velune.intelligence.graph_patcher import KnowledgeGraphPatcher
from velune.knowledge.graph import KnowledgeGraph
from velune.repository.incremental_indexer import IndexDelta
from velune.repository.tracker import GitTracker


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "a@b.c")
    _git(tmp_path, "config", "user.name", "tester")
    return tmp_path


# ── Co-change clustering ────────────────────────────────────────────────


def test_files_committed_together_repeatedly_form_a_cluster(git_repo):
    (git_repo / "a.py").write_text("a\n", encoding="utf-8")
    (git_repo / "b.py").write_text("b\n", encoding="utf-8")
    (git_repo / "c.py").write_text("c\n", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "init")

    for i in range(3):
        (git_repo / "a.py").write_text(f"a{i}\n", encoding="utf-8")
        (git_repo / "b.py").write_text(f"b{i}\n", encoding="utf-8")
        _git(git_repo, "commit", "-q", "-am", f"update a+b {i}")

    tracker = GitTracker(git_repo)
    clusters = tracker.get_co_change_clusters(days=90, min_shared_commits=2)

    pair_map = {frozenset((a, b)): count for a, b, count in clusters}
    assert frozenset(("a.py", "b.py")) in pair_map
    assert pair_map[frozenset(("a.py", "b.py"))] >= 3
    # c.py never changed alongside anything — no pair involving it.
    assert not any("c.py" in (a, b) for a, b, _ in clusters)


def test_large_commit_does_not_pollute_clusters(git_repo):
    """A commit touching more files than max_files_per_commit (a repo-wide
    sweep) must not connect every pair of files it touched."""
    files = [f"f{i}.py" for i in range(25)]
    for name in files:
        (git_repo / name).write_text("x\n", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "huge init commit touching 25 files")

    tracker = GitTracker(git_repo)
    clusters = tracker.get_co_change_clusters(
        days=90, min_shared_commits=1, max_files_per_commit=20
    )
    assert clusters == []


def test_non_git_repo_returns_empty_cluster_list(tmp_path):
    tracker = GitTracker(tmp_path)
    assert tracker.get_co_change_clusters() == []


# ── Churn-weighted confidence decay ─────────────────────────────────────


async def _patch(root, delta):
    graph = KnowledgeGraph(root / ".velune" / "kg.db")
    await graph.initialize()
    patcher = KnowledgeGraphPatcher(graph, root)
    await patcher.patch(delta)
    return graph


def test_frequently_changed_file_has_lower_confidence_than_stable_one(git_repo):
    (git_repo / "stable.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (git_repo / "churny.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "init")

    for i in range(10):
        (git_repo / "churny.py").write_text(f"def g():\n    return {i}\n", encoding="utf-8")
        _git(git_repo, "commit", "-q", "-am", f"update {i}")

    async def run():
        graph = await _patch(git_repo, IndexDelta(to_add=["stable.py", "churny.py"]))
        stable = await graph.get_node("file:stable.py")
        churny = await graph.get_node("file:churny.py")
        return stable, churny

    stable, churny = asyncio.run(run())
    assert churny.confidence < stable.confidence
    assert stable.confidence <= 1.0
    assert churny.confidence >= 0.5  # never decays below the floor


def test_file_with_zero_recent_commits_gets_full_confidence(git_repo):
    """A file that has never appeared in a commit within the volatility
    window (e.g. brand new, uncommitted) isn't penalized — only files that
    actually show up in git log's history get discounted."""
    (git_repo / "committed.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(git_repo, "add", "committed.py")
    _git(git_repo, "commit", "-q", "-m", "init")

    # Written to disk but never committed — absent from git log entirely.
    (git_repo / "untouched.py").write_text("def g():\n    return 1\n", encoding="utf-8")

    async def run():
        graph = await _patch(git_repo, IndexDelta(to_add=["untouched.py"]))
        return await graph.get_node("file:untouched.py")

    node = asyncio.run(run())
    assert node.confidence == 1.0


def test_non_git_workspace_does_not_discount_confidence(tmp_path):
    (tmp_path / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    async def run():
        graph = await _patch(tmp_path, IndexDelta(to_add=["x.py"]))
        return await graph.get_node("file:x.py")

    node = asyncio.run(run())
    assert node.confidence == 1.0


# ── Rename lineage metadata ──────────────────────────────────────────────


def test_parse_file_records_lineage_metadata_when_renamed_from_is_given(git_repo):
    """Unit-level check of _parse_file's own renamed_from handling, decoupled
    from how patch() derives the argument from delta.renames (covered
    end-to-end by the next test)."""
    (git_repo / "old_name.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (git_repo / "old_name.py").rename(git_repo / "new_name.py")

    patcher = KnowledgeGraphPatcher(graph=None, workspace_root=git_repo)
    nodes, _ = patcher._parse_file("new_name.py", renamed_from="old_name.py")

    file_node = next(n for n in nodes if n.file_path == "new_name.py")
    assert file_node.metadata["renamed_from"] == "old_name.py"
    assert "rename_lineage" in file_node.provenance


async def _patch_with_result(root, delta, renamed_from=None):
    graph = KnowledgeGraph(root / ".velune" / "kg.db")
    await graph.initialize()
    patcher = KnowledgeGraphPatcher(graph, root)
    result = await patcher.patch(delta)
    return graph, result


def test_full_rename_delta_through_patch_records_lineage(git_repo):
    (git_repo / "old_name.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "init")

    (git_repo / "old_name.py").rename(git_repo / "new_name.py")

    delta = IndexDelta(
        to_add=["new_name.py"], to_remove=["old_name.py"], renames=[("old_name.py", "new_name.py")]
    )

    async def run():
        graph, result = await _patch_with_result(git_repo, delta)
        new_node = await graph.get_node("file:new_name.py")
        old_node = await graph.get_node("file:old_name.py")
        return result, new_node, old_node

    result, new_node, old_node = asyncio.run(run())
    assert old_node is None  # cleanly removed, not left as a dangling stale node
    assert new_node is not None
    assert new_node.metadata.get("renamed_from") == "old_name.py"
    assert "rename_lineage" in new_node.provenance
