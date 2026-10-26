"""Workspace search is bounded, safe against pathological patterns, and off the event loop.

Covers ``grep_files`` / ``find_files`` and the ``symbol_search`` fallback (audit
finding TUI-02 and the unbounded-output / ReDoS / raw-regex-error defects).
All fixtures are temporary repositories; nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from velune.execution.path_guard import PathTraversalError
from velune.tools.code.search import SymbolSearch
from velune.tools.filesystem import search as search_mod
from velune.tools.filesystem.search import (
    FIND_MAX_RESULTS,
    GREP_MAX_MATCHES,
    FindFiles,
    GrepFiles,
)


def _marker(results: list) -> dict | None:
    return results[-1] if results and results[-1].get("truncated") else None


def _hits(results: list) -> list:
    return [r for r in results if not r.get("truncated")]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "billing.py").write_text(
        "def total(x):\n    return x + tax(x)\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "util.py").write_text("def tax(x):\n    return x * 0.2\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("remember the tax rules\n", encoding="utf-8")
    (tmp_path / "node_modules" / "junk").mkdir(parents=True)
    (tmp_path / "node_modules" / "junk" / "index.js").write_text("tax();\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "x.py").write_text("tax = 1\n", encoding="utf-8")
    (tmp_path / "ignored_dir").mkdir()
    (tmp_path / "ignored_dir" / "hidden.py").write_text("tax = 2\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TAX_SECRET=FAKE_NOT_REAL\n", encoding="utf-8")
    return tmp_path


async def heartbeat_gap(coro) -> tuple[object, float]:
    """Run *coro* while a 10 ms ticker runs; return (result, longest stall in seconds)."""
    worst = 0.0

    async def tick():
        nonlocal worst
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst = max(worst, now - last - 0.01)
            last = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.05)
    try:
        result = await coro
        await asyncio.sleep(0.03)  # let a stalled ticker record the stall
    finally:
        ticker.cancel()
    return result, worst


# ── 1. normal searches still work ────────────────────────────────────────────


async def test_content_search_finds_matching_lines_with_provenance(repo):
    results = await GrepFiles(workspace=repo).execute("tax")
    files = {Path(r["file"]).name for r in _hits(results)}
    assert files == {"billing.py", "util.py", "notes.txt"}
    billing = next(r for r in results if r["file"].endswith("billing.py"))
    assert billing["line"] == 2 and "tax(x)" in billing["text"] and billing["match"] == "tax"
    assert _marker(results) is None  # complete search: no truncation marker


async def test_search_respects_ignore_rules_and_secret_files(repo):
    results = await GrepFiles(workspace=repo).execute("tax")
    joined = " ".join(r["file"] for r in _hits(results))
    for excluded in ("node_modules", ".venv", "ignored_dir", ".env"):
        assert excluded not in joined
    secret = await GrepFiles(workspace=repo).execute("FAKE_NOT_REAL")
    assert _hits(secret) == []


async def test_file_pattern_narrows_the_search(repo):
    results = await GrepFiles(workspace=repo).execute("tax", file_pattern="*.py")
    assert {Path(r["file"]).suffix for r in _hits(results)} == {".py"}


async def test_filename_search_prunes_ignored_directories(repo):
    (repo / ".veluneignore").write_text("ignored_dir/\n", encoding="utf-8")
    found = await FindFiles(workspace=repo).execute("*.py")
    names = sorted(Path(p).name for p in found)
    assert names == ["billing.py", "util.py"]  # not .venv/x.py, not ignored_dir/hidden.py
    assert await FindFiles(workspace=repo).execute("index.js") == []  # inside node_modules


async def test_find_files_does_not_descend_into_ignored_directories(repo, monkeypatch):
    import os

    visited: list[str] = []
    real_walk = os.walk

    def spy(top, *a, **kw):
        for dirpath, dirnames, filenames in real_walk(top, *a, **kw):
            visited.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(search_mod.os, "walk", spy)
    await FindFiles(workspace=repo).execute("*.py")
    assert not any("node_modules" in d or ".venv" in d for d in visited)


# ── 2. output is bounded and says so ─────────────────────────────────────────


async def test_broad_pattern_cannot_produce_an_unbounded_response(tmp_path):
    for n in range(5):
        (tmp_path / f"f{n}.txt").write_text("line\n" * 1000, encoding="utf-8")
    results = await GrepFiles(workspace=tmp_path).execute(".")
    assert 0 < len(_hits(results)) <= GREP_MAX_MATCHES
    marker = _marker(results)
    # Whichever bound bites first (count, or output size on long paths) is named.
    assert marker and any(r.startswith(("max_matches=", "output_limit")) for r in marker["reasons"])


async def test_output_size_is_bounded_even_when_match_count_is_not(tmp_path):
    (tmp_path / "wide.txt").write_text(("x" * 1500 + "\n") * 200, encoding="utf-8")
    results = await GrepFiles(workspace=tmp_path).execute("x")
    size = sum(len(str(r)) for r in results)
    assert size < 40_000
    assert _marker(results) is not None


async def test_oversized_files_are_skipped_and_reported_not_silently_ignored(tmp_path):
    (tmp_path / "big.txt").write_text("needle\n" * 400_000, encoding="utf-8")  # ~2.8 MB
    (tmp_path / "small.txt").write_text("needle\n", encoding="utf-8")
    results = await GrepFiles(workspace=tmp_path).execute("needle")
    assert [Path(r["file"]).name for r in _hits(results)] == ["small.txt"]
    marker = _marker(results)
    assert marker and any(r.startswith("skipped_files_over_") for r in marker["reasons"])
    assert marker["skipped_large"] == 1


async def test_binary_files_are_not_searched(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01needle\x02")
    (tmp_path / "blob.py").write_bytes(b"\x00needle")
    results = await GrepFiles(workspace=tmp_path).execute("needle")
    assert _hits(results) == []


async def test_find_files_result_count_is_bounded(tmp_path):
    for n in range(FIND_MAX_RESULTS + 50):
        (tmp_path / f"m{n:04d}.py").write_text("", encoding="utf-8")
    results = await FindFiles(workspace=tmp_path).execute("*.py")
    paths = [r for r in results if isinstance(r, str)]
    assert len(paths) == FIND_MAX_RESULTS
    assert results[-1]["truncated"] and any(
        r.startswith("max_results=") for r in results[-1]["reasons"]
    )
    # deterministic: same call, same answer
    assert results == await FindFiles(workspace=tmp_path).execute("*.py")


async def test_file_count_limit_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "GREP_MAX_FILES", 3)
    for n in range(10):
        (tmp_path / f"f{n}.txt").write_text("needle\n", encoding="utf-8")
    results = await GrepFiles(workspace=tmp_path).execute("needle")
    assert len(_hits(results)) == 3
    assert any(r.startswith("file_limit=") for r in _marker(results)["reasons"])


# ── 3. regex safety ──────────────────────────────────────────────────────────


async def test_invalid_regex_is_a_controlled_error(repo):
    with pytest.raises(ValueError, match="Invalid regular expression"):
        await GrepFiles(workspace=repo).execute("([")


async def test_pathological_regex_returns_within_the_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "GREP_TIME_BUDGET_SECONDS", 2.0)
    (tmp_path / "evil.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
    start = time.perf_counter()
    results = await GrepFiles(workspace=tmp_path).execute(r"^(a+)+$")
    elapsed = time.perf_counter() - start
    assert elapsed < 6.0, f"pathological regex held the call for {elapsed:.1f}s"
    marker = _marker(results)
    assert marker and any(r.startswith("time_budget=") for r in marker["reasons"])


async def test_event_loop_stays_responsive_during_a_pathological_search(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "GREP_TIME_BUDGET_SECONDS", 2.0)
    (tmp_path / "evil.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
    _, worst = await heartbeat_gap(GrepFiles(workspace=tmp_path).execute(r"^(a+)+$"))
    assert worst < 0.25, f"event loop stalled for {worst * 1000:.0f} ms"


async def test_event_loop_stays_responsive_during_ordinary_searches(tmp_path):
    for d in range(20):
        sub = tmp_path / f"d{d}"
        sub.mkdir()
        for n in range(50):
            (sub / f"m{n}.py").write_text("def f():\n    return 1\n" * 200, encoding="utf-8")
    _, worst_grep = await heartbeat_gap(GrepFiles(workspace=tmp_path).execute("return 2"))
    _, worst_find = await heartbeat_gap(FindFiles(workspace=tmp_path).execute("m4*.py"))
    assert worst_grep < 0.25, f"grep stalled the loop for {worst_grep * 1000:.0f} ms"
    assert worst_find < 0.25, f"find stalled the loop for {worst_find * 1000:.0f} ms"


async def test_cancellation_kills_the_search_worker_and_returns_promptly(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "GREP_TIME_BUDGET_SECONDS", 30.0)
    (tmp_path / "evil.txt").write_text("a" * 40 + "!\n", encoding="utf-8")

    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class Recording(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            spawned.append(self)

    monkeypatch.setattr(search_mod.subprocess, "Popen", Recording)

    task = asyncio.create_task(GrepFiles(workspace=tmp_path).execute(r"^(a+)+$"))
    await asyncio.sleep(1.0)
    workers = [p for p in spawned if "_grep_worker" in str(p.args)]
    assert workers and workers[0].poll() is None  # the worker is busy backtracking
    start = time.perf_counter()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.perf_counter() - start < 2.0
    for _ in range(50):
        if workers[0].poll() is not None:
            break
        await asyncio.sleep(0.05)
    assert workers[0].poll() is not None, "the search worker outlived the cancelled call"


async def test_next_search_works_after_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "GREP_TIME_BUDGET_SECONDS", 1.5)
    (tmp_path / "evil.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("needle here\n", encoding="utf-8")
    timed_out = await GrepFiles(workspace=tmp_path).execute(r"^(a+)+$")
    assert _marker(timed_out) is not None
    again = await GrepFiles(workspace=tmp_path).execute("needle")
    assert [Path(r["file"]).name for r in _hits(again)] == ["ok.txt"]


# ── 4. workspace boundary ────────────────────────────────────────────────────


async def test_search_cannot_leave_the_workspace(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("tax\n", encoding="utf-8")
    for tool, args in (
        (GrepFiles(workspace=repo), ("tax",)),
        (FindFiles(workspace=repo), ("*.txt",)),
    ):
        with pytest.raises(PathTraversalError):
            await tool.execute(*args, directory="..")
        with pytest.raises(PathTraversalError):
            await tool.execute(*args, directory=str(outside))


async def test_default_directory_is_the_workspace_not_the_process_cwd(
    repo, tmp_path_factory, monkeypatch
):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.chdir(elsewhere)
    grep = await GrepFiles(workspace=repo).execute("tax")
    find = await FindFiles(workspace=repo).execute("billing.py")
    assert len(_hits(grep)) == 3
    assert [Path(p).name for p in find] == ["billing.py"]


# ── 5. symbol_search fallback ────────────────────────────────────────────────


async def test_symbol_search_fallback_is_off_loop_and_correct(repo):
    tool = SymbolSearch(workspace=repo, container=_EmptyContainer())
    results, worst = await heartbeat_gap(tool.execute("tax"))
    names = {(Path(r["file"]).name, r["kind"]) for r in results}
    assert ("util.py", "function") in names
    assert worst < 0.25


async def test_symbol_search_fallback_reports_a_file_limit(repo, monkeypatch):
    from velune.tools.code import search as code_search

    monkeypatch.setattr(code_search, "SYMBOL_SCAN_MAX_FILES", 1)
    results = await SymbolSearch(workspace=repo, container=_EmptyContainer()).execute("tax")
    assert results and results[-1].get("truncated")


class _EmptyContainer:
    """Stands in for a runtime whose knowledge graph is not (yet) available."""

    def has(self, key: str) -> bool:
        return False

    def get(self, key: str):  # pragma: no cover
        raise KeyError(key)
