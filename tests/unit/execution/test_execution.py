import pytest
import time
import os
from pathlib import Path
from velune.execution.sandbox import SubprocessSandbox, SandboxResult
from velune.core.errors.execution import SandboxError
from velune.execution.checkpointer import FileCheckpointer
from velune.execution.rollback import RollbackManager
from velune.repository.tracker import GitTracker

def test_subprocess_sandbox_safe_commands(tmp_path):
    sandbox = SubprocessSandbox(tmp_path)
    
    # Executing a simple safe python command
    res = sandbox.execute("python -c \"print('Hello from sandbox')\"")
    assert res.exit_code == 0
    assert "Hello from sandbox" in res.stdout
    assert res.duration_ms > 0
    
    # Attempting to run an unsafe command should raise SandboxError
    with pytest.raises(SandboxError, match="contains blocked pattern"):
        sandbox.execute("rm -rf /")

def test_subprocess_sandbox_outside_workspace(tmp_path):
    sandbox = SubprocessSandbox(tmp_path)
    
    # Spawning in a directory outside the workspace should raise SandboxError
    outside_dir = Path("C:/Windows") if os.name == "nt" else Path("/etc")
    with pytest.raises(SandboxError, match="outside workspace"):
        sandbox.execute("python -c \"print('hello')\"", cwd=outside_dir)

def test_file_checkpointer_and_rollback(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    checkpointer = FileCheckpointer(workspace)
    
    # 1. Create a dummy file
    file_a = workspace / "file_a.txt"
    file_a.write_text("Version 1")
    
    # 2. Checkpoint state
    checkpoint_data = checkpointer.create_checkpoint("chk_1", [file_a])
    assert "copied_files" in checkpoint_data
    assert checkpoint_data["copied_files"]["file_a.txt"] is not None
    
    # 3. Modify file
    file_a.write_text("Version 2")
    
    # 4. Restore state
    checkpointer.restore_checkpoint("chk_1", checkpoint_data)
    assert file_a.read_text() == "Version 1"

def test_rollback_manager_new_file_cleanup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    git_tracker = GitTracker(workspace)
    # Force git active status to False to avoid relying on actual Git repository in unit tests
    git_tracker.is_git = False
    
    rollback_mgr = RollbackManager(workspace, git_tracker)
    
    file_existing = workspace / "existing.txt"
    file_existing.write_text("original")
    
    file_new = workspace / "new_file.txt"
    
    # Capture checkpoint BEFORE new_file is created
    checkpoint_data = rollback_mgr.save_state("chk_2", [file_existing, file_new])
    
    # Create the new file and modify existing file
    file_new.write_text("created in sandboxed step")
    file_existing.write_text("modified")
    
    # Trigger rollback
    rollback_mgr.rollback(checkpoint_data)
    
    # Verify rollback states
    assert file_existing.read_text() == "original"
    assert not file_new.exists()  # Newly created file should be cleaned up!
