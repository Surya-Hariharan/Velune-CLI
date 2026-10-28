"""The real council edit path, in a throwaway git repository.

``apply_council_edits`` parses the coder's SEARCH/REPLACE blocks, writes the accepted
files and (only when opted in) commits them. These tests drive that whole path, and
``/undo`` after it, against a temporary repository -- never the project's own.
"""

from __future__ import annotations

import asyncio
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from velune.cli.handlers import council as council_handlers
from velune.cli.handlers.council import apply_council_edits
from velune.cli.handlers.git import cmd_undo
from velune.kernel.config import ExecutionConfig

PROPOSAL = "edited.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "edited.py").write_text("x = 1\n", encoding="utf-8")
    (root / "staged.py").write_text("s = 1\n", encoding="utf-8")
    (root / "unstaged.py").write_text("u = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    # the user's own work in progress
    (root / "staged.py").write_text("s = 2\n", encoding="utf-8")
    _git(root, "add", "staged.py")
    (root / "unstaged.py").write_text("u = 2\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _auto_accept(monkeypatch):
    async def allow(repl, file_writes):
        return True  # policy is covered elsewhere; this file is about what happens next

    monkeypatch.setattr(council_handlers, "_authorize_council_edits", allow)


def _repl(repo: Path, *, auto_commit: bool | None) -> SimpleNamespace:
    config = None
    if auto_commit is not None:
        config = SimpleNamespace(execution=ExecutionConfig(council_auto_commit=auto_commit))
    container = SimpleNamespace(
        get=lambda key: repo,
        get_optional=lambda key, default=None: config if key == "runtime.config" else default,
    )
    return SimpleNamespace(
        container=container,
        console=Console(file=StringIO(), force_terminal=False, width=120),
        active_model=None,
        _hunk_review_mode=False,
    )


def _status(repo: Path) -> dict[str, str]:
    return {line[3:]: line[:2] for line in _git(repo, "status", "--porcelain").splitlines() if line}


async def test_by_default_the_edit_is_written_but_not_committed(repo):
    before = _head(repo)

    await apply_council_edits(_repl(repo, auto_commit=None), PROPOSAL, "bump x")

    assert (repo / "edited.py").read_text(encoding="utf-8") == "x = 2\n"
    assert _head(repo) == before
    assert _status(repo) == {"edited.py": " M", "staged.py": "M ", "unstaged.py": " M"}


async def test_an_explicit_false_also_does_not_commit(repo):
    before = _head(repo)
    await apply_council_edits(_repl(repo, auto_commit=False), PROPOSAL, "bump x")
    assert _head(repo) == before


async def test_opting_in_commits_only_the_edited_file(repo):
    before = _head(repo)

    await apply_council_edits(_repl(repo, auto_commit=True), PROPOSAL, "bump x")

    assert _git(repo, "rev-list", "--count", f"{before}..HEAD").strip() == "1"
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["edited.py"]
    # the user's staged and unstaged work is exactly as they left it
    assert _status(repo) == {"staged.py": "M ", "unstaged.py": " M"}
    assert (repo / "staged.py").read_text(encoding="utf-8") == "s = 2\n"
    assert (repo / "unstaged.py").read_text(encoding="utf-8") == "u = 2\n"
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["staged.py"]


async def test_cancelling_during_review_writes_and_commits_nothing(repo, monkeypatch):
    async def cancelled(self, file_writes, auto_accept=False):
        raise asyncio.CancelledError

    monkeypatch.setattr("velune.execution.multi_diff.MultiDiffPreview.preview_batch", cancelled)
    before = _head(repo)

    with pytest.raises(asyncio.CancelledError):
        await apply_council_edits(_repl(repo, auto_commit=True), PROPOSAL, "bump x")

    assert _head(repo) == before
    assert (repo / "edited.py").read_text(encoding="utf-8") == "x = 1\n"


async def test_rejecting_the_diff_commits_nothing(repo, monkeypatch):
    from velune.execution.diff_preview import DiffDecision

    async def reject(self, file_writes, auto_accept=False):
        return dict.fromkeys(file_writes, DiffDecision.REJECT)

    monkeypatch.setattr("velune.execution.multi_diff.MultiDiffPreview.preview_batch", reject)
    before = _head(repo)

    await apply_council_edits(_repl(repo, auto_commit=True), PROPOSAL, "bump x")

    assert _head(repo) == before
    assert (repo / "edited.py").read_text(encoding="utf-8") == "x = 1\n"


async def test_a_failing_commit_is_reported_not_raised_and_creates_no_commit(repo, monkeypatch):
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1, "", "hook rejected")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    before = _head(repo)

    await apply_council_edits(_repl(repo, auto_commit=True), PROPOSAL, "bump x")

    assert _head(repo) == before
    assert (repo / "edited.py").read_text(encoding="utf-8") == "x = 2\n"  # the edit itself stands


# ── /undo ────────────────────────────────────────────────────────────────────


async def test_undo_reverts_the_council_commit_and_keeps_the_changes_staged(repo):
    base = _head(repo)
    await apply_council_edits(_repl(repo, auto_commit=True), PROPOSAL, "bump x")
    assert _head(repo) != base

    await cmd_undo(_repl(repo, auto_commit=True), "")

    assert _head(repo) == base
    assert (repo / "edited.py").read_text(encoding="utf-8") == "x = 2\n"
    assert "edited.py" in _git(repo, "diff", "--cached", "--name-only").split()


async def test_undo_refuses_a_commit_velune_did_not_make(repo):
    _git(repo, "commit", "-q", "-m", "my own commit")
    mine = _head(repo)
    repl = _repl(repo, auto_commit=True)

    await cmd_undo(repl, "")

    assert _head(repo) == mine
    assert "not a Velune-generated commit" in repl.console.file.getvalue()
