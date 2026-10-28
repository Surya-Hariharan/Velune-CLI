"""The turn path reads cached repository state; edits must reach those caches.

``IncrementalIndexer.apply_delta`` only updates ``index_state.json``. The prompt
path reads the symbol/file snapshot (``index_cache.json``) and the BM25 corpus
(``retrieval_index.json``), which only a full ``index()`` used to rewrite, so after
an edit, rename or delete the model was retrieving a repository that no longer
existed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from velune.repository.cognition import RepositoryCognitionService
from velune.repository.incremental_indexer import IncrementalIndexer
from velune.retrieval.hybrid import HybridRetriever


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "alpha.py").write_text("def alpha_fn():\n    return 1\n", encoding="utf-8")
    (root / "beta.py").write_text("def beta_fn():\n    return 2\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _edit(repo: Path) -> None:
    """Modify one file, delete one, add one."""
    (repo / "alpha.py").write_text(
        "def alpha_fn():\n    return 1\n\ndef gamma_new():\n    return 3\n", encoding="utf-8"
    )
    (repo / "beta.py").unlink()
    (repo / "delta.py").write_text("def delta_fn():\n    return 4\n", encoding="utf-8")


def _assert_fresh(service: RepositoryCognitionService, repo: Path) -> None:
    snapshot = service.get_snapshot()
    assert snapshot is not None
    assert sorted(f.path for f in snapshot.files) == ["alpha.py", "delta.py"]
    names = {s.name for s in snapshot.symbols}
    assert {"alpha_fn", "gamma_new", "delta_fn"} <= names
    assert "beta_fn" not in names

    corpus = json.loads((repo / ".velune" / "retrieval_index.json").read_text(encoding="utf-8"))
    by_id = {d["id"]: d["content"] for d in corpus}
    assert sorted(by_id) == ["alpha.py", "delta.py"]
    assert "gamma_new" in by_id["alpha.py"]


async def test_background_apply_refreshes_snapshot_and_retrieval_index(repo):
    service = RepositoryCognitionService(repo)
    service.index(force=True)
    _edit(repo)

    assert await service.probe_for_changes()
    await service._bg_index_task

    _assert_fresh(service, repo)


async def test_run_incremental_refreshes_snapshot_and_retrieval_index(repo):
    service = RepositoryCognitionService(repo)
    service.index(force=True)
    _edit(repo)

    await service.run_incremental()

    _assert_fresh(service, repo)


async def test_engine_pipeline_refresh_sees_the_edited_files(repo):
    """The intelligence engine applies the delta itself, then asks for the pipeline refresh."""
    service = RepositoryCognitionService(repo)
    service.index(force=True)
    _edit(repo)

    inc = IncrementalIndexer(repo, service._state_path)
    delta = await inc.compute_delta()
    await inc.apply_delta(delta)
    await service.refresh_pipeline_cache(delta)

    _assert_fresh(service, repo)
    fresh = await service.get_snapshot_fresh()
    assert sorted(f.path for f in fresh.files) == ["alpha.py", "delta.py"]


def _write_corpus(path: Path, docs: list[tuple[str, str]]) -> None:
    path.write_text(
        json.dumps([{"id": i, "content": c, "metadata": {"path": i}} for i, c in docs]),
        encoding="utf-8",
    )


def test_retriever_drops_its_corpus_when_every_file_is_deleted(tmp_path):
    corpus = tmp_path / "retrieval_index.json"
    _write_corpus(corpus, [("alpha.py", "alpha.py alpha_fn python alpha.py")])
    retriever = HybridRetriever()
    retriever.bind_lexical_index(corpus, loaded=False)
    assert retriever.refresh_lexical_index()
    assert len(retriever.lexical_retriever.documents) == 1

    _write_corpus(corpus, [])  # the last indexed file was deleted
    assert retriever.refresh_lexical_index()

    assert retriever.lexical_retriever.documents == []


def test_retriever_reloads_a_rewrite_that_keeps_the_same_mtime(tmp_path):
    import os

    corpus = tmp_path / "retrieval_index.json"
    _write_corpus(corpus, [("alpha.py", "alpha.py alpha_fn")])
    retriever = HybridRetriever()
    retriever.bind_lexical_index(corpus, loaded=False)
    assert retriever.refresh_lexical_index()
    stamp = corpus.stat()

    _write_corpus(corpus, [("alpha.py", "alpha.py alpha_fn"), ("delta.py", "delta.py delta_fn")])
    os.utime(corpus, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # same tick as the first write

    assert retriever.refresh_lexical_index()
    assert {d.id for d in retriever.lexical_retriever.documents} == {"alpha.py", "delta.py"}


# ── edge cases: empty repo, repeated changes, failures ───────────────────────


def _corpus_ids(repo: Path) -> list[str]:
    path = repo / ".velune" / "retrieval_index.json"
    return sorted(d["id"] for d in json.loads(path.read_text(encoding="utf-8")))


async def test_an_empty_repository_gains_its_first_file(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q")
    service = RepositoryCognitionService(root)
    service.index(force=True)
    assert service.get_snapshot().files == []
    assert _corpus_ids(root) == []

    (root / "first.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    assert await service.probe_for_changes()
    await service._bg_index_task

    assert [f.path for f in service.get_snapshot().files] == ["first.py"]
    assert _corpus_ids(root) == ["first.py"]


async def test_the_last_file_being_deleted_empties_both_stores(repo):
    service = RepositoryCognitionService(repo)
    service.index(force=True)
    (repo / "alpha.py").unlink()
    (repo / "beta.py").unlink()

    assert await service.probe_for_changes()
    await service._bg_index_task

    assert service.get_snapshot().files == []
    assert _corpus_ids(repo) == []


async def test_repeated_changes_each_leave_the_stores_consistent(repo):
    service = RepositoryCognitionService(repo)
    service.index(force=True)
    expected = {"alpha.py", "beta.py"}

    steps = [
        lambda: (repo / "alpha.py").write_text("def a2():\n    return 2\n", encoding="utf-8"),
        lambda: (repo / "beta.py").unlink(),
        lambda: (repo / "beta.py").write_text("def b_again():\n    return 5\n", encoding="utf-8"),
        lambda: (repo / "gamma.py").write_text("def g():\n    return 6\n", encoding="utf-8"),
        lambda: (repo / "alpha.py").unlink(),
    ]
    for step in steps:
        step()
        expected = {p.name for p in repo.glob("*.py")}
        await service.run_incremental()
        snapshot = service.get_snapshot()
        assert {f.path for f in snapshot.files} == expected
        assert set(_corpus_ids(repo)) == expected
        assert {s.name for s in snapshot.symbols} == {
            n for p in expected for n in _symbols_on_disk(repo / p)
        }


def _symbols_on_disk(path: Path) -> set[str]:
    return {
        line.split("def ")[1].split("(")[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("def ")
    }


async def test_a_failed_refresh_keeps_the_last_valid_state_and_does_not_raise(repo, monkeypatch):
    service = RepositoryCognitionService(repo)
    service.index(force=True)
    cache = repo / ".velune" / "index_cache.json"
    corpus = repo / ".velune" / "retrieval_index.json"
    before = (cache.read_bytes(), corpus.read_bytes())
    _edit(repo)

    def explode(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(service.indexer, "index", explode)

    delta = await service.run_incremental()  # the caller still gets its delta

    assert delta is not None
    assert (cache.read_bytes(), corpus.read_bytes()) == before


def _fail_replace_for(monkeypatch, filename: str) -> None:
    import os

    real = os.replace

    def replace(src, dst, *args, **kwargs):
        if Path(dst).name == filename:
            raise OSError("rename failed")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def test_a_failed_retrieval_index_write_keeps_the_previous_file_and_leaves_no_temp(
    repo, monkeypatch
):
    service = RepositoryCognitionService(repo)
    snapshot = service.index(force=True)
    corpus = repo / ".velune" / "retrieval_index.json"
    before = corpus.read_bytes()
    _edit(repo)
    snapshot = service.indexer.index()

    _fail_replace_for(monkeypatch, "retrieval_index.json")
    service._save_retrieval_index(snapshot)

    assert corpus.read_bytes() == before
    assert list(corpus.parent.glob("*.tmp")) == []


def test_a_failed_snapshot_cache_write_keeps_the_previous_file_and_leaves_no_temp(
    repo, monkeypatch
):
    service = RepositoryCognitionService(repo)
    service.index(force=True)
    cache = repo / ".velune" / "index_cache.json"
    before = cache.read_bytes()
    _edit(repo)

    _fail_replace_for(monkeypatch, "index_cache.json")
    service.indexer.index()

    assert cache.read_bytes() == before
    assert list(cache.parent.glob("*.tmp")) == []
    assert service.get_snapshot() is not None  # the old cache still loads
