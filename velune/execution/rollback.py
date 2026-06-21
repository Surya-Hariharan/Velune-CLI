"""Rollback manager utilizing git checkouts and local snapshot restores."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from velune.core.errors.execution import RollbackError
from velune.execution.checkpointer import FileCheckpointer
from velune.repository.tracker import GitTracker

logger = logging.getLogger("velune.execution.rollback")


class RollbackManager:
    """Orchestrates checkpoint saving and target rollback on execution failure."""

    def __init__(self, workspace_path: Path, git_tracker: GitTracker | None = None) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.checkpointer = FileCheckpointer(self.workspace_path)
        self.git_tracker = git_tracker or GitTracker(self.workspace_path)

    def save_state(self, checkpoint_id: str, files_to_track: list[Path]) -> dict[str, Any]:
        """Save the workspace state for files before a command execution."""
        from velune.execution.path_guard import validate_workspace_path

        for file in files_to_track:
            abs_file = (
                Path(file).resolve()
                if Path(file).is_absolute()
                else (self.workspace_path / file).resolve()
            )
            validate_workspace_path(abs_file, self.workspace_path, "tracked file")

        # 1. Gather file snapshots
        file_snapshot = self.checkpointer.create_checkpoint(checkpoint_id, files_to_track)

        # 2. Add git state tracking if git is active
        git_active = self.git_tracker.is_git
        git_stash_success = False
        if git_active:
            # We can create a stash snapshot as an extra safety measure
            git_stash_success = self.git_tracker.create_stash(f"velune-pre-{checkpoint_id}")
            if git_stash_success:
                # Apply it back so files remain visible for execution
                # We keep the stash entry active in git stash list for transaction safety
                self.git_tracker.apply_stash()

        return {
            "checkpoint_id": checkpoint_id,
            "file_snapshot": file_snapshot,
            "git_active": git_active,
            "git_stash_success": git_stash_success,
        }

    def rollback(self, checkpoint_data: dict[str, Any]) -> None:
        """Rolls back the workspace state to the captured checkpoint."""
        checkpoint_id = checkpoint_data.get("checkpoint_id", "")
        logger.warning("Triggering state rollback for checkpoint: %s", checkpoint_id)

        # 1. Perform git-level cleanup first if git is active (extremely fast and safe)
        if checkpoint_data.get("git_active"):
            try:
                logger.info(
                    "Executing Git rollback: resetting tracked files and cleaning untracked artifacts..."
                )
                self.git_tracker._run_git(["reset", "--hard", "HEAD"])
                self.git_tracker._run_git(["clean", "-fd"])
                if checkpoint_data.get("git_stash_success"):
                    logger.info("Restoring pre-execution uncommitted changes from stash...")
                    self.git_tracker.pop_stash()
                logger.info("Git rollback and workspace cleaning successfully completed.")
            except Exception as e:
                logger.warning(
                    "Git reset/clean rollback failed, attempting file-based copy recovery: %s", e
                )

        # 2. Restore file snapshots (acts as primary recovery for non-git or fallback for git failures)
        file_snapshot = checkpoint_data.get("file_snapshot")
        if file_snapshot:
            try:
                self.checkpointer.restore_checkpoint(checkpoint_id, file_snapshot)
                logger.info("File copy recovery successfully completed.")
            except Exception as e:
                logger.error("Snapshot restore failed during rollback: %s", e)
                raise RollbackError(f"File rollback copy recovery failed: {e}")

        logger.info("Successfully reverted workspace to pre-execution state for %s", checkpoint_id)
