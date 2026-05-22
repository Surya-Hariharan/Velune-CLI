"""Rollback manager utilizing git checkouts and local snapshot restores."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List, Optional
import logging

from velune.core.errors.execution import RollbackError
from velune.execution.checkpointer import FileCheckpointer
from velune.repository.tracker import GitTracker

logger = logging.getLogger("velune.execution.rollback")


class RollbackManager:
    """Orchestrates checkpoint saving and target rollback on execution failure."""

    def __init__(self, workspace_path: Path, git_tracker: Optional[GitTracker] = None) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.checkpointer = FileCheckpointer(self.workspace_path)
        self.git_tracker = git_tracker or GitTracker(self.workspace_path)

    def save_state(self, checkpoint_id: str, files_to_track: List[Path]) -> Dict[str, Any]:
        """Save the workspace state for files before a command execution."""
        # 1. Gather file snapshots
        file_snapshot = self.checkpointer.create_checkpoint(checkpoint_id, files_to_track)
        
        # 2. Add git state tracking if git is active
        git_active = self.git_tracker.is_git
        git_stash_success = False
        if git_active:
            # We can create a stash snapshot as an extra safety measure
            git_stash_success = self.git_tracker.create_stash(f"velune-pre-{checkpoint_id}")
            if git_stash_success:
                # Immediately pop it back so files remain visible for execution
                # We just want a stash to exist in git log if we need to hard reset to it
                self.git_tracker.pop_stash()

        return {
            "checkpoint_id": checkpoint_id,
            "file_snapshot": file_snapshot,
            "git_active": git_active,
            "git_stash_success": git_stash_success,
        }

    def rollback(self, checkpoint_data: Dict[str, Any]) -> None:
        """Rolls back the workspace state to the captured checkpoint."""
        checkpoint_id = checkpoint_data.get("checkpoint_id", "")
        logger.warning("Triggering state rollback for checkpoint: %s", checkpoint_id)

        # 1. Restore the file snapshot copies
        file_snapshot = checkpoint_data.get("file_snapshot")
        if file_snapshot:
            try:
                self.checkpointer.restore_checkpoint(checkpoint_id, file_snapshot)
            except Exception as e:
                logger.error("Snapshot restore failed during rollback: %s", e)
                raise RollbackError(f"File rollback failed: {e}")

        # 2. Perform git-level cleanup if git is active
        if checkpoint_data.get("git_active"):
            try:
                # Discard unstaged modifications for check-pointed files
                copied_files = file_snapshot.get("copied_files", {}) if file_snapshot else {}
                for rel_path_str in copied_files.keys():
                    abs_path = self.workspace_path / rel_path_str
                    if abs_path.exists():
                        self.git_tracker._run_git(["checkout", "--", str(abs_path)])
                logger.info("Discarded uncommitted git modifications for tracked files")
            except Exception as e:
                logger.warning("Git checkout rollback failed: %s", e)

        logger.info("Successfully reverted workspace to pre-execution state for %s", checkpoint_id)
