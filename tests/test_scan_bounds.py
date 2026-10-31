"""File discovery honours the same deadline and cancellation as the search that uses it.

``grep_files`` advertised a 10 s budget, but the directory walk and the
``git ls-files`` call that precede the search had neither a deadline nor a way to
stop, so a large tree (or ``/project close``, which roots the workspace at the home
directory) ran far past it and kept running after the call was cancelled.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from velune.repository.scanner import FilesystemScanner
from velune.tools.filesystem import search as search_mod
from velune.tools.filesystem.search import FindFiles, GrepFiles


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for d in range(40):
        pkg = tmp_path / f"pkg{d}"
        pkg.mkdir()
        for f in range(25):
            (pkg / f"m{f}.py").write_text(f"needle_{d}_{f} = 1\n", encoding="utf-8")
    return tmp_path


def test_a_scan_without_limits_still_returns_every_file(tree):
    scanner = FilesystemScanner(tree)
    assert len(scanner.scan([".py"])) == 40 * 25
    assert scanner.truncated is False


def test_an_expired_deadline_stops_the_walk_and_says_so(tree):
    scanner = FilesystemScanner(tree)
    started = time.monotonic()

    files = scanner.scan([".py"], deadline=time.monotonic() - 1)

    assert time.monotonic() - started < 1.0
    assert len(files) < 40 * 25
    assert scanner.truncated is True


def test_a_set_cancel_flag_stops_the_walk(tree):
    cancel = threading.Event()
    cancel.set()
    scanner = FilesystemScanner(tree)

    files = scanner.scan([".py"], cancel=cancel)

    assert len(files) < 40 * 25
    assert scanner.truncated is True


def test_git_ls_files_is_bounded_by_a_timeout(tree, monkeypatch):
    seen: list[object] = []

    def fake_run(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout") or 0)

    monkeypatch.setattr("subprocess.run", fake_run)

    FilesystemScanner(tree)  # a hung git must not hang the constructor, nor break it

    assert seen and all(isinstance(t, (int, float)) and t > 0 for t in seen)


async def test_grep_hands_its_deadline_and_cancel_flag_to_discovery(tree, monkeypatch):
    """A walk that would run for a minute must be told when to stop, and stop."""
    monkeypatch.setattr(search_mod, "GREP_TIME_BUDGET_SECONDS", 0.2)
    handed: dict[str, object] = {}

    def slow_scan(self, extensions=None, *, deadline=None, cancel=None):
        handed.update(deadline=deadline, cancel=cancel)
        end = time.monotonic() + 60  # a tree that takes a minute to walk
        while time.monotonic() < end:
            if (deadline is not None and time.monotonic() > deadline) or (
                cancel is not None and cancel.is_set()
            ):
                self.truncated = True
                break
            time.sleep(0.01)
        return []

    monkeypatch.setattr(FilesystemScanner, "scan", slow_scan)
    started = time.monotonic()

    results = await GrepFiles(workspace=tree).execute("needle")

    assert time.monotonic() - started < 5.0
    assert handed["deadline"] is not None and handed["cancel"] is not None
    assert results[-1].get("truncated") is True
    assert any(r.startswith("time_budget") for r in results[-1]["reasons"])


async def test_find_files_checks_its_deadline_inside_a_single_large_directory(
    tmp_path, monkeypatch
):
    big = tmp_path / "big"
    big.mkdir()
    for i in range(3000):
        (big / f"f{i}.txt").write_text("", encoding="utf-8")

    ticks = iter(range(10_000_000))

    class Clock:  # each reading advances 0.2 s, so a 1 s budget is spent after ~5 reads
        @staticmethod
        def monotonic() -> float:
            return next(ticks) * 0.2

    monkeypatch.setattr(search_mod, "time", Clock)
    monkeypatch.setattr(search_mod, "FIND_TIME_BUDGET_SECONDS", 1.0)

    # nothing matches, so only the deadline (not the result cap) can end the walk
    results = await FindFiles(workspace=tmp_path).execute("*.nomatch")

    assert results and results[-1].get("truncated") is True, (
        "the deadline was only checked between directories"
    )
    assert any(r.startswith("time_budget") for r in results[-1]["reasons"])


async def test_cancelling_a_grep_stops_the_discovery_thread_not_just_the_coroutine(
    tree, monkeypatch
):
    exited = threading.Event()

    def endless_scan(self, extensions=None, *, deadline=None, cancel=None):
        try:
            while not (cancel is not None and cancel.is_set()):
                time.sleep(0.01)  # a walk with no deadline of its own
        finally:
            exited.set()
        return []

    monkeypatch.setattr(FilesystemScanner, "scan", endless_scan)
    task = asyncio.create_task(GrepFiles(workspace=tree).execute("needle"))
    await asyncio.sleep(0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(exited.wait, 3.0), "the walk kept running after the cancel"


def test_an_unreadable_directory_does_not_abort_the_scan(tree, monkeypatch):
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "pkg3":
            raise PermissionError("access denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    scanner = FilesystemScanner(tree)

    files = scanner.scan([".py"])

    assert len(files) == 39 * 25  # every directory but the locked one
    assert not any("pkg3" in f.parts for f in files)
    assert scanner.truncated is False  # unreadable is not the same as out of time


def test_a_git_ls_files_that_outlives_its_timeout_is_killed(tree, monkeypatch):
    """subprocess.run must be handed a real timeout and must not leave the child running."""
    import psutil

    # The parent learns the child's pid from Popen itself. The child must not have to start
    # and report in before the timeout: under load an interpreter can take longer than the
    # 0.5 s timeout to come up, and it would be killed before it could write anything.
    real_run, real_popen = subprocess.run, subprocess.Popen
    spawned: list[subprocess.Popen] = []

    class SpyPopen(real_popen):  # type: ignore[valid-type, misc]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    def run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            cmd = [sys.executable, "-c", "import time; time.sleep(60)"]  # a git that never returns
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", SpyPopen)
    started = time.monotonic()

    FilesystemScanner(tree, git_timeout=0.5)

    assert time.monotonic() - started < 10
    assert spawned, "the scanner never started the stand-in git"
    for proc in spawned:
        assert proc.poll() is not None, "the timed-out child process was not reaped"
        assert not psutil.pid_exists(proc.pid), "the timed-out child process is still running"
