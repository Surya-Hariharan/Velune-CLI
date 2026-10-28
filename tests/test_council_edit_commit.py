"""Council edits are committed only when the user opted in, and only the edited files.

The commit used to run unconditionally and as a plain ``git commit``, which also
swept up anything the user had already staged for a different change.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.cli.handlers.council import _auto_commit_edits, _council_auto_commit_enabled
from velune.kernel.config import ExecutionConfig


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
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
    (root / "unrelated.py").write_text("y = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _repl(execution) -> SimpleNamespace:
    config = SimpleNamespace(execution=execution) if execution is not None else None
    return SimpleNamespace(container=SimpleNamespace(get_optional=lambda key: config))


def test_auto_commit_is_off_by_default():
    assert ExecutionConfig().council_auto_commit is False
    assert _council_auto_commit_enabled(_repl(ExecutionConfig())) is False


def test_auto_commit_is_off_when_no_config_is_available():
    assert _council_auto_commit_enabled(_repl(None)) is False


def test_auto_commit_can_be_opted_into():
    assert _council_auto_commit_enabled(_repl(ExecutionConfig(council_auto_commit=True))) is True


async def test_commit_contains_only_the_edited_paths(repo):
    (repo / "edited.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "unrelated.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "unrelated.py")  # the user's own staged change

    assert await _auto_commit_edits(_repl(None), [repo / "edited.py"], "tweak x", repo)

    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["edited.py"]
    assert "Co-authored-by: Velune Council" in _git(repo, "log", "-1", "--format=%B")
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["unrelated.py"]


async def test_commit_includes_a_new_file(repo):
    (repo / "added.py").write_text("z = 1\n", encoding="utf-8")

    assert await _auto_commit_edits(_repl(None), [repo / "added.py"], "add z", repo)

    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["added.py"]
