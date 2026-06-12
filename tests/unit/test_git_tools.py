"""Unit tests for asynchronous Git tools and batched volatility checks (Batch 07)."""

from __future__ import annotations

import asyncio
import time
import subprocess
from pathlib import Path

import pytest

from velune.repository.tracker import GitTracker
from velune.tools.git.history import GitLog
from velune.tools.git.state import GitStatus


def _init_git_repo(path: Path) -> None:
    """Initialize a git repository in the given directory."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, check=True)


def _make_commit(path: Path, msg: str, files: dict[str, str]) -> None:
    """Write files and commit them to the repository."""
    for rel_path, content in files.items():
        file_path = path / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", rel_path], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Fixture supplying a valid Git repository with initial commits."""
    _init_git_repo(tmp_path)
    _make_commit(tmp_path, "Initial commit", {"README.md": "# Test Repo\n"})
    return tmp_path


@pytest.mark.asyncio
async def test_git_log_is_nonblocking(git_repo: Path):
    """Verify GitLog.execute() is a coroutine function and extracts history correctly."""
    tool = GitLog(workspace=git_repo)

    import inspect
    # 1. Assert execute is a coroutine function
    assert inspect.iscoroutinefunction(tool.execute)

    # Add another commit to verify log output
    _make_commit(git_repo, "Second commit", {"src/main.py": "print('hello')\n"})

    # 2. Run execute and verify results
    commits = await tool.execute(directory=str(git_repo), limit=5)
    
    assert isinstance(commits, list)
    assert len(commits) >= 2
    assert commits[0]["message"] == "Second commit"
    assert commits[1]["message"] == "Initial commit"
    assert "hash" in commits[0]
    assert "author" in commits[0]
    assert "date" in commits[0]


def test_batch_volatility_matches_per_file(git_repo: Path):
    """Create commits on multiple files, verify batched volatility matches individual volatility counts."""
    tracker = GitTracker(git_repo)
    
    # Create 5 files with multiple commits altering different files
    _make_commit(git_repo, "Commit 2", {"file1.txt": "v2", "file2.txt": "v2"})
    _make_commit(git_repo, "Commit 3", {"file2.txt": "v3", "file3.txt": "v3", "file4.txt": "v3"})
    _make_commit(git_repo, "Commit 4", {"file1.txt": "v4", "file4.txt": "v4", "file5.txt": "v4"})

    files = ["file1.txt", "file2.txt", "file3.txt", "file4.txt", "file5.txt"]
    
    # Get batched counts
    batch_counts = tracker.get_all_file_volatility(days=90)
    
    # Compare each file individually
    for f in files:
        individual_count = tracker.get_file_volatility(f, days=90)
        batched_count = batch_counts.get(f, 0)
        assert individual_count == batched_count, f"Volatility mismatch for file '{f}'"


def test_batch_volatility_performance(git_repo: Path):
    """Verify get_all_file_volatility() performance on a large number of files completing in < 2 seconds."""
    tracker = GitTracker(git_repo)
    
    # Populate 50 separate files and make commits
    bulk_files = {f"src/file_{i}.py": f"print({i})" for i in range(50)}
    _make_commit(git_repo, "Bulk commit", bulk_files)
    
    # Measure execution time
    start = time.perf_counter()
    batch_vol = tracker.get_all_file_volatility(days=90)
    duration = time.perf_counter() - start
    
    assert duration < 2.0, f"Batched volatility took {duration:.4f}s - must be < 2s"
    assert len(batch_vol) >= 50


@pytest.mark.asyncio
async def test_git_tool_does_not_block_event_loop(git_repo: Path):
    """Verify GitStatus.execute() does not block the asyncio event loop."""
    tool = GitStatus(workspace=git_repo)

    # Add files to untracked space to give the status command work
    for i in range(10):
        (git_repo / f"untracked_{i}.txt").write_text("untracked", encoding="utf-8")

    # Set up a concurrent task that checks event-loop latency
    loop_blocked = False
    
    async def loop_monitor():
        nonlocal loop_blocked
        start = time.perf_counter()
        await asyncio.sleep(0.1)  # Expected wait
        elapsed = time.perf_counter() - start
        # If event loop is blocked, this sleep will take much longer
        if elapsed > 0.2:
            loop_blocked = True

    # Execute both concurrently
    await asyncio.gather(
        loop_monitor(),
        tool.execute(directory=str(git_repo))
    )

    assert not loop_blocked, "The event loop was blocked by the GitStatus.execute call."


def test_rollback_manager_preserves_uncommitted_changes(git_repo: Path):
    """Verify that RollbackManager.rollback() preserves pre-existing uncommitted changes when restoring target files."""
    from velune.execution.rollback import RollbackManager
    
    # 1. Setup pre-existing uncommitted changes (tracked modify + untracked new file)
    readme_path = git_repo / "README.md"
    readme_path.write_text("# Test Repo\nUncommitted user change\n", encoding="utf-8")
    
    untracked_path = git_repo / "untracked_user_file.txt"
    untracked_path.write_text("untracked user content", encoding="utf-8")
    
    # Target file we plan to edit and track in rollback
    target_path = git_repo / "src" / "main.py"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("original target content", encoding="utf-8")
    
    # Commit the target file so it's tracked
    subprocess.run(["git", "add", "src/main.py"], cwd=git_repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Add target"], cwd=git_repo, capture_output=True, check=True)
    
    # 2. Instantiate RollbackManager and save state before a failed sandbox command
    manager = RollbackManager(git_repo)
    checkpoint_state = manager.save_state(
        checkpoint_id="test-cp-1",
        files_to_track=[Path("src/main.py")]
    )
    
    assert checkpoint_state["git_active"] is True
    assert checkpoint_state["git_stash_success"] is True
    
    # 3. Modify target file (simulating the failed command execution)
    target_path.write_text("failed command changes", encoding="utf-8")
    
    # Create an untracked command artifact
    artifact_path = git_repo / "failed_command_artifact.tmp"
    artifact_path.write_text("should be deleted by clean", encoding="utf-8")
    
    # 4. Trigger rollback
    manager.rollback(checkpoint_state)
    
    # 5. Assertions
    # Tracked failed modifications should be reverted to original
    assert target_path.read_text(encoding="utf-8") == "original target content"
    
    # Failed command untracked artifact should be deleted
    assert not artifact_path.exists()
    
    # PRE-EXISTING UNCOMMITTED CHANGES MUST BE FULLY PRESERVED!
    assert readme_path.read_text(encoding="utf-8") == "# Test Repo\nUncommitted user change\n"
    assert untracked_path.exists()
    assert untracked_path.read_text(encoding="utf-8") == "untracked user content"
