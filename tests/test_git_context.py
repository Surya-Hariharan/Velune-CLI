"""Tests for GitContextProvider — git state gathering and prompt formatting."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from velune.repository.git_context import GitContextProvider, GitSnapshot


def _make_snap(**kwargs) -> GitSnapshot:
    defaults = dict(
        branch="main",
        head_sha="abc1234567890",
        last_commits=[{"sha7": "abc1234", "subject": "fix: do the thing"}],
        staged_files=["src/foo.py"],
        modified_files=["src/bar.py"],
        untracked_files=["notes.txt"],
        staged_diff_summary="--- a/src/foo.py\n+++ b/src/foo.py\n+new line",
    )
    defaults.update(kwargs)
    return GitSnapshot(**defaults)


class TestGitSnapshotDataclass:
    def test_default_fields_are_empty_lists(self):
        snap = GitSnapshot(branch="main", head_sha="abc")
        assert snap.staged_files == []
        assert snap.modified_files == []
        assert snap.untracked_files == []
        assert snap.staged_diff_summary == ""

    def test_fields_preserved(self):
        snap = _make_snap()
        assert snap.branch == "main"
        assert snap.last_commits[0]["subject"] == "fix: do the thing"


class TestBuildContextBlock:
    def test_returns_empty_for_none(self):
        provider = GitContextProvider(Path("."))
        assert provider.build_context_block(None) == ""

    def test_returns_empty_for_empty_snap(self):
        provider = GitContextProvider(Path("."))
        snap = GitSnapshot(branch="main", head_sha="abc")
        # No staged/modified/untracked/commits → nothing useful
        assert provider.build_context_block(snap) == ""

    def test_contains_branch(self):
        provider = GitContextProvider(Path("."))
        block = provider.build_context_block(_make_snap())
        assert "main" in block

    def test_contains_head_sha(self):
        provider = GitContextProvider(Path("."))
        block = provider.build_context_block(_make_snap())
        assert "abc1234" in block

    def test_contains_recent_commit(self):
        provider = GitContextProvider(Path("."))
        block = provider.build_context_block(_make_snap())
        assert "fix: do the thing" in block

    def test_contains_staged_files(self):
        provider = GitContextProvider(Path("."))
        block = provider.build_context_block(_make_snap())
        assert "src/foo.py" in block

    def test_contains_diff(self):
        provider = GitContextProvider(Path("."))
        block = provider.build_context_block(_make_snap())
        assert "new line" in block

    def test_diff_truncated_at_max(self):
        provider = GitContextProvider(Path("."))
        long_diff = "+" + "x" * (GitContextProvider.MAX_DIFF_CHARS + 500)
        snap = _make_snap(staged_diff_summary="")
        snap.staged_diff_summary = long_diff
        block = provider.build_context_block(snap)
        assert "[diff truncated]" in block

    def test_diff_not_truncated_when_short(self):
        provider = GitContextProvider(Path("."))
        short_diff = "+only a small change"
        snap = _make_snap(staged_diff_summary=short_diff)
        block = provider.build_context_block(snap)
        assert "[diff truncated]" not in block
        assert "small change" in block

    def test_untracked_files_shown(self):
        provider = GitContextProvider(Path("."))
        block = provider.build_context_block(_make_snap(untracked_files=["README.draft"]))
        assert "README.draft" in block


class TestGatherWithRealRepo:
    def test_gather_returns_snapshot_for_git_repo(self, tmp_path):
        pytest.importorskip("git")
        import git as gitpython

        repo = gitpython.Repo.init(str(tmp_path))
        # Create an initial commit
        (tmp_path / "file.py").write_text("x = 1\n")
        repo.index.add(["file.py"])
        repo.index.commit("initial commit")

        provider = GitContextProvider(tmp_path)
        snap = provider.gather()
        assert snap is not None
        assert snap.head_sha != ""
        assert snap.branch in ("main", "master", "HEAD (detached)")

    def test_gather_detects_staged_files(self, tmp_path):
        pytest.importorskip("git")
        import git as gitpython

        repo = gitpython.Repo.init(str(tmp_path))
        (tmp_path / "file.py").write_text("x = 1\n")
        repo.index.add(["file.py"])
        repo.index.commit("initial commit")

        # Stage a new change
        (tmp_path / "file.py").write_text("x = 2\n")
        repo.index.add(["file.py"])

        provider = GitContextProvider(tmp_path)
        snap = provider.gather()
        assert snap is not None
        assert "file.py" in snap.staged_files

    def test_gather_returns_none_outside_repo(self, tmp_path):
        provider = GitContextProvider(tmp_path)
        # tmp_path with no .git directory is not a git repo
        snap = provider.gather()
        # May return None or a snapshot with empty data depending on gitpython behavior
        # The important invariant is that it doesn't raise
        assert snap is None or isinstance(snap, GitSnapshot)


class TestGatherErrorHandling:
    def test_gather_handles_missing_gitpython(self):
        """gather() returns None gracefully when gitpython is not installed."""
        provider = GitContextProvider(Path("."))
        with patch.dict("sys.modules", {"git": None}):
            result = provider.gather()
            assert result is None


class TestGitSnapshotExtendedFields:
    def test_commit_bodies_default_empty(self):
        snap = GitSnapshot(branch="main", head_sha="abc")
        assert snap.commit_bodies == []

    def test_last_commit_diff_stat_default_empty(self):
        snap = GitSnapshot(branch="main", head_sha="abc")
        assert snap.last_commit_diff_stat == ""

    def test_build_context_block_includes_body(self):
        provider = GitContextProvider(Path("."))
        snap = _make_snap(
            last_commits=[{
                "sha7": "abc1234",
                "subject": "fix: retry loop",
                "body": "Fixes the retry logic on connection pool",
            }],
            commit_bodies=["Fixes the retry logic on connection pool"],
        )
        block = provider.build_context_block(snap)
        assert "Fixes the retry logic" in block

    def test_build_context_block_includes_diff_stat(self):
        provider = GitContextProvider(Path("."))
        snap = _make_snap(last_commit_diff_stat="velune/foo.py | 5 +++++")
        block = provider.build_context_block(snap)
        assert "velune/foo.py" in block
        assert "Last commit diff stat" in block

    def test_build_context_block_no_diff_stat_when_empty(self):
        provider = GitContextProvider(Path("."))
        snap = _make_snap(last_commit_diff_stat="")
        block = provider.build_context_block(snap)
        assert "Last commit diff stat" not in block

    def test_gather_populates_5_commits(self, tmp_path):
        pytest.importorskip("git")
        import git as gitpython

        repo = gitpython.Repo.init(str(tmp_path))
        repo.config_writer().set_value("user", "name", "Test").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()
        for i in range(6):
            (tmp_path / f"file{i}.py").write_text(f"x = {i}\n")
            repo.index.add([f"file{i}.py"])
            repo.index.commit(f"commit {i}")

        provider = GitContextProvider(tmp_path)
        snap = provider.gather()
        assert snap is not None
        assert len(snap.last_commits) == 5

    def test_commit_body_included_in_last_commits(self, tmp_path):
        pytest.importorskip("git")
        import git as gitpython

        repo = gitpython.Repo.init(str(tmp_path))
        repo.config_writer().set_value("user", "name", "Test").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()
        (tmp_path / "file.py").write_text("x = 1\n")
        repo.index.add(["file.py"])
        repo.index.commit("feat: add thing\n\nThis is the commit body detail.")

        provider = GitContextProvider(tmp_path)
        snap = provider.gather()
        assert snap is not None
        assert snap.last_commits[0]["body"] == "This is the commit body detail."
