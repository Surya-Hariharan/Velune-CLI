"""Regressions for defects found auditing the repository index.

Each test here corresponds to something that was silently broken in production:

* the API-connection map raised on every run and the exception was swallowed,
  so the feature had never once emitted a route into a prompt;
* ``git diff HEAD`` cannot see untracked files, so brand-new code was invisible
  to the indexer indefinitely;
* ``IndexState.save`` truncated in place, so a reader racing a writer got a
  torn file, parsed it as ``None``, and re-indexed the whole repo from scratch;
* nothing recorded mtime/size, so every delta re-hashed the entire tree.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from velune.repository.cognition import RepositoryCognitionService
from velune.repository.incremental_indexer import IncrementalIndexer
from velune.repository.index_state import IndexedFile, IndexState
from velune.repository.schemas import RepositorySnapshot


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# The API map
# ---------------------------------------------------------------------------


def test_snapshot_accepts_an_api_map():
    """RepositorySnapshot is a Pydantic v2 model, so assigning an undeclared
    attribute raises. `snapshot.api_map = ...` did exactly that, on every run."""
    snapshot = RepositorySnapshot(root_path=".")
    snapshot.api_map = {"routes": []}  # must not raise
    assert snapshot.api_map == {"routes": []}


def test_api_map_is_populated_for_a_repo_with_routes(tmp_path):
    """The end-to-end assertion that was missing: a repo with a route must
    produce a route in the snapshot summary."""
    (tmp_path / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        '@app.get("/users/{user_id}")\n'
        "def get_user(user_id: int):\n"
        "    return {}\n"
    )

    snapshot = RepositoryCognitionService(tmp_path).index(force=True)

    assert snapshot.api_map is not None
    assert snapshot.summary["api_map"]["route_count"] == 1
    assert [r.path for r in snapshot.api_map.routes] == ["/users/{user_id}"]


# ---------------------------------------------------------------------------
# Untracked files
# ---------------------------------------------------------------------------


def test_untracked_file_makes_the_tree_dirty(git_repo):
    """`git diff HEAD` does not report untracked files, so the old check called
    a tree with brand-new code in it "clean" — and the caller then took a fast
    path that returned an empty delta without touching the disk."""
    inc = IncrementalIndexer(git_repo, git_repo / ".velune" / "index_state.json")
    assert inc.working_tree_is_clean() is True

    (git_repo / "brand_new.py").write_text("def b():\n    return 2\n")

    assert inc.working_tree_is_clean() is False


async def test_untracked_file_appears_in_the_delta(git_repo):
    state_path = git_repo / ".velune" / "index_state.json"
    inc = IncrementalIndexer(git_repo, state_path)

    await inc.apply_delta(await inc.compute_delta())
    assert (await inc.compute_delta()).is_empty  # settled

    (git_repo / "brand_new.py").write_text("def b():\n    return 2\n")

    delta = await inc.compute_delta()
    assert "brand_new.py" in delta.to_add


# ---------------------------------------------------------------------------
# IndexState persistence
# ---------------------------------------------------------------------------


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / ".velune" / "index_state.json"
    state = IndexState.empty(str(tmp_path))
    state.save(path)

    assert json.loads(path.read_text())["workspace_root"] == str(tmp_path)
    # os.replace means the temp file is consumed, never left behind
    assert list(path.parent.glob("*.tmp")) == []


def test_a_reader_never_sees_a_half_written_file(tmp_path):
    """The failure this prevents: a torn read returns None, and None is
    indistinguishable from "no state yet" — so the caller concludes it is a
    first run and re-indexes the entire repository."""
    path = tmp_path / ".velune" / "index_state.json"

    big = IndexState.empty(str(tmp_path))
    for i in range(2000):
        big.update_file(
            IndexedFile(
                path=f"f{i}.py",
                content_hash="x" * 64,
                language="python",
                symbol_count=1,
                indexed_at=time.time(),
            )
        )
    big.save(path)

    # Overwrite repeatedly while reading; every read must yield a complete state.
    for _ in range(5):
        big.save(path)
        loaded = IndexState.load(path)
        assert loaded is not None
        assert len(loaded.file_index) == 2000


def test_mtime_and_size_survive_a_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = IndexState.empty(str(tmp_path))
    state.update_file(
        IndexedFile(
            path="a.py",
            content_hash="abc",
            language="python",
            symbol_count=1,
            indexed_at=1.0,
            mtime=123.5,
            size=42,
        )
    )
    state.save(path)

    entry = IndexState.load(path).file_index["a.py"]
    assert (entry.mtime, entry.size) == (123.5, 42)


def test_state_written_before_mtime_existed_still_loads(tmp_path):
    """Old on-disk entries have no mtime/size. They must load (defaulting to 0)
    and must NOT be treated as unchanged, or we'd skip a file we never hashed."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "workspace_root": str(tmp_path),
                "last_commit_sha": None,
                "last_indexed_at": 0.0,
                "file_index": {
                    "a.py": {
                        "path": "a.py",
                        "content_hash": "abc",
                        "language": "python",
                        "symbol_count": 1,
                        "indexed_at": 0.0,
                    }
                },
            }
        )
    )

    entry = IndexState.load(path).file_index["a.py"]
    assert (entry.mtime, entry.size) == (0.0, 0)

    real = tmp_path / "a.py"
    real.write_text("x = 1\n")
    assert entry.unchanged_on_disk(real.stat()) is False


# ---------------------------------------------------------------------------
# The mtime/size prefilter
# ---------------------------------------------------------------------------


async def test_unchanged_files_are_not_rehashed(git_repo, monkeypatch):
    """The whole point of the prefilter: with a dirty tree (the normal state of
    development) the change-detection loop fires every 3s, and each pass used to
    read and SHA-256 every file in the repository."""
    state_path = git_repo / ".velune" / "index_state.json"
    inc = IncrementalIndexer(git_repo, state_path)
    await inc.apply_delta(await inc.compute_delta())

    # Make the tree dirty so the git fast path cannot short-circuit the walk.
    (git_repo / "untracked.py").write_text("y = 2\n")
    await inc.apply_delta(await inc.compute_delta())

    hashed: list[str] = []
    original = IncrementalIndexer._hash_file

    def _spy(self, path):
        hashed.append(path.name)
        return original(self, path)

    monkeypatch.setattr(IncrementalIndexer, "_hash_file", _spy)

    delta = await inc.compute_delta()

    assert delta.is_empty
    assert hashed == [], f"re-hashed unchanged files: {hashed}"


async def test_a_modified_file_is_still_detected(git_repo):
    """The prefilter must not be so eager that it misses a real edit."""
    state_path = git_repo / ".velune" / "index_state.json"
    inc = IncrementalIndexer(git_repo, state_path)
    await inc.apply_delta(await inc.compute_delta())

    time.sleep(0.01)
    (git_repo / "a.py").write_text("def a():\n    return 999  # changed\n")

    delta = await inc.compute_delta()
    assert "a.py" in delta.to_update


# ---------------------------------------------------------------------------
# Incremental pipeline cache (Phase 3): a no-op prompt must not re-run the
# grapher/API-mapper/architecture pipeline, and an edit must only rescan the
# files that actually changed.
# ---------------------------------------------------------------------------


async def test_get_snapshot_fresh_skips_recompute_when_nothing_changed(git_repo, monkeypatch):
    service = RepositoryCognitionService(git_repo)

    first = await service.get_snapshot_fresh()
    assert first is not None
    assert service.pipeline_cache_misses == 1  # cold start: no cache yet

    calls: list[int] = []
    original = RepositoryCognitionService._run_pipeline

    def _spy(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RepositoryCognitionService, "_run_pipeline", _spy)

    second = await service.get_snapshot_fresh()

    assert second is not None
    assert calls == [], "second call re-ran the pipeline despite an unchanged workspace"
    assert service.pipeline_cache_hits == 1


async def test_get_snapshot_fresh_cache_carries_edges_and_api_map(git_repo):
    (git_repo / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        '@app.get("/items")\n'
        "def list_items():\n"
        "    return []\n"
    )

    service = RepositoryCognitionService(git_repo)
    first = await service.get_snapshot_fresh()
    assert first.summary["api_map"]["route_count"] == 1

    second = await service.get_snapshot_fresh()
    assert second.api_map is not None
    assert [r.path for r in second.api_map.routes] == ["/items"]
    assert second.summary["cognition_freshness"]["cache_hit"] is True


async def test_refresh_pipeline_cache_scopes_api_mapper_to_changed_files(git_repo, monkeypatch):
    """The background pipeline_refresh path must re-scan only the delta's
    files, not the whole workspace, once a baseline pipeline cache exists."""
    from velune.repository.api_mapper import APIMapper

    service = RepositoryCognitionService(git_repo)
    await service.get_snapshot_fresh()  # cold start: seeds the cache + grapher

    (git_repo / "b.py").write_text("def b():\n    return 2\n")

    state_path = git_repo / ".velune" / "index_state.json"
    inc = IncrementalIndexer(git_repo, state_path)
    delta = await inc.compute_delta()
    assert "b.py" in delta.to_add
    await inc.apply_delta(delta)

    scanned: list[str] = []
    original_scan = APIMapper.scan_file

    def _spy(self, rel_path):
        scanned.append(rel_path)
        return original_scan(self, rel_path)

    monkeypatch.setattr(APIMapper, "scan_file", _spy)

    stats = await service.refresh_pipeline_cache(delta)

    assert scanned == ["b.py"], f"rescanned more than the delta: {scanned}"
    assert stats["files_recomputed"] == 1
    assert service.files_recomputed_last_run == 1


async def test_refresh_pipeline_cache_removes_stale_graph_nodes(git_repo):
    service = RepositoryCognitionService(git_repo)
    await service.get_snapshot_fresh()  # seeds grapher with a.py

    assert "a.py" in service.grapher.graph

    (git_repo / "a.py").unlink()
    state_path = git_repo / ".velune" / "index_state.json"
    inc = IncrementalIndexer(git_repo, state_path)
    delta = await inc.compute_delta()
    assert "a.py" in delta.to_remove
    await inc.apply_delta(delta)

    await service.refresh_pipeline_cache(delta)

    assert "a.py" not in service.grapher.graph


async def test_refresh_pipeline_cache_without_baseline_does_a_full_build(tmp_path):
    """If the background engine's change-detection loop fires before any
    cold-start index has ever run, refresh_pipeline_cache must fall back to a
    full build instead of patching an empty graph."""
    from velune.repository.incremental_indexer import IndexDelta

    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")

    service = RepositoryCognitionService(tmp_path)
    service.index(force=True)  # populate the file/symbol cache only, no pipeline cache yet
    assert service._load_pipeline_cache() is None

    stats = await service.refresh_pipeline_cache(IndexDelta(to_add=["a.py"]))

    assert stats["files_recomputed"] == 2  # full build saw both files, not just the delta
    assert service._load_pipeline_cache() is not None
