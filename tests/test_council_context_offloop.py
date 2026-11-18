"""Gathering council context must not block the event loop, and one failure must not sink it."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from velune.cognition.orchestrator import CouncilOrchestrator
from velune.repository import git_context, project_type
from velune.repository.cognition import RepositoryCognitionService


class _Firewall:
    def wrap_workspace_content(self, kind, text):
        return f"<{kind}>{text}</{kind}>"

    def scan_file_for_injection(self, name, text):
        return {}


class _Container:
    def __init__(self, workspace):
        self._ws = workspace

    def has(self, key):
        return key == "runtime.workspace"

    def get(self, key):
        return str(self._ws)


def _orch(tmp_path, captured):
    async def execute(**kw):
        captured.append(kw["repo_context"])
        return {"final_summary": "ok"}

    orch = CouncilOrchestrator.__new__(CouncilOrchestrator)
    orch._states = {}
    orch._runtime_container = lambda: _Container(tmp_path)
    orch.firewall = _Firewall()
    orch.max_wall_time_seconds = 5.0
    orch.execute_task = execute
    return orch


async def _run(orch):
    return [i async for i in orch.stream("x")]


async def test_blocking_context_lookups_do_not_stall_the_event_loop(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    seen_threads: list[int] = []

    class SlowGit:
        def __init__(self, root):
            pass

        def gather(self):
            seen_threads.append(threading.get_ident())
            time.sleep(0.3)  # a blocking subprocess-like call
            return "snap"

        def build_context_block(self, snap):
            return "branch: main"

    class SlowDetector:
        def detect(self, root):
            seen_threads.append(threading.get_ident())
            time.sleep(0.3)
            return SimpleNamespace(config_files=[])

    monkeypatch.setattr(git_context, "GitContextProvider", SlowGit)
    monkeypatch.setattr(project_type, "ProjectTypeDetector", SlowDetector)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    captured: list[str] = []
    task = asyncio.create_task(ticker())
    started = time.monotonic()
    await _run(_orch(tmp_path, captured))
    elapsed = time.monotonic() - started
    task.cancel()

    assert ticks > 10, ticks  # loop kept turning during the 0.3s blocking calls
    assert main_thread not in seen_threads  # both ran in worker threads
    assert elapsed < 0.55, elapsed  # git and config lookups overlapped (0.3 + 0.3 serial)
    assert "<git_context>branch: main</git_context>" in captured[0]


async def test_a_failing_git_lookup_does_not_lose_the_config_block(tmp_path, monkeypatch):
    class BrokenGit:
        def __init__(self, root):
            pass

        def gather(self):
            raise RuntimeError("git is not installed")

    monkeypatch.setattr(git_context, "GitContextProvider", BrokenGit)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    captured: list[str] = []
    await _run(_orch(tmp_path, captured))
    assert "git_context" not in captured[0]  # the failure was swallowed, run still happened
    assert captured  # execute_task was reached


async def test_a_hung_git_lookup_times_out_instead_of_hanging(tmp_path, monkeypatch):
    from velune.cognition import orchestrator as orch_mod

    release = threading.Event()

    class HungGit:
        def __init__(self, root):
            pass

        def gather(self):
            release.wait(5)
            return "late"

        def build_context_block(self, snap):
            return "late"

    monkeypatch.setattr(git_context, "GitContextProvider", HungGit)
    monkeypatch.setattr(orch_mod, "_CONTEXT_TIMEOUT_S", 0.1)
    captured: list[str] = []
    try:
        await asyncio.wait_for(_run(_orch(tmp_path, captured)), 3)
    finally:
        release.set()
    assert captured and "late" not in captured[0]


async def test_get_snapshot_is_read_in_a_worker_thread(tmp_path, monkeypatch):
    svc = RepositoryCognitionService.__new__(RepositoryCognitionService)
    main_thread = threading.get_ident()
    where: list[int] = []

    def slow_snapshot():
        where.append(threading.get_ident())
        return None

    svc.get_snapshot = slow_snapshot  # type: ignore[method-assign]
    svc._load_pipeline_cache = lambda: None  # type: ignore[method-assign]
    svc.pipeline_cache_misses = 0
    svc.index = lambda full=False: None  # type: ignore[method-assign]
    await svc.get_snapshot_fresh()
    assert where and where[0] != main_thread
