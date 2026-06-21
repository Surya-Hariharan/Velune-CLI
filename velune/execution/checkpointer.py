"""File checkpointer for backing up and restoring targeted file states."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from velune.core.errors.execution import SnapshotError
from velune.execution.path_guard import validate_workspace_path

logger = logging.getLogger("velune.execution.checkpointer")


class FileCheckpointer:
    """Manages file backups and state restoration in a local workspace subdirectory."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.snapshots_dir = self.workspace_path / ".velune" / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def create_checkpoint(self, checkpoint_id: str, files_to_track: list[Path]) -> dict[str, Any]:
        """Create a backup of the current state of files.

        Tracks whether files exist, copying their contents to a safe snapshots folder.
        """
        checkpoint_path = self.snapshots_dir / checkpoint_id
        checkpoint_path.mkdir(parents=True, exist_ok=True)

        copied_files: dict[str, Any] = {}
        for file in files_to_track:
            try:
                if not Path(file).is_absolute():
                    abs_file = (self.workspace_path / file).resolve()
                else:
                    abs_file = Path(file).resolve()
            except Exception as e:
                raise SnapshotError(f"Failed to resolve path {file}: {e}")

            # Validate path is within workspace
            try:
                validate_workspace_path(abs_file, self.workspace_path, "tracked file")
            except ValueError as e:
                logger.error("Path containment violation in checkpoint: %s", e)
                raise

            try:
                if not abs_file.exists():
                    # Keep record that file didn't exist so rollback deletes it
                    rel_str = str(abs_file.relative_to(self.workspace_path)).replace("\\", "/")
                    copied_files[rel_str] = None
                    continue

                rel_path = abs_file.relative_to(self.workspace_path)
                rel_str = str(rel_path).replace("\\", "/")

                backup_file = checkpoint_path / rel_path
                backup_file.parent.mkdir(parents=True, exist_ok=True)

                shutil.copy2(abs_file, backup_file)
                backup_rel_str = str(backup_file.relative_to(self.workspace_path)).replace(
                    "\\", "/"
                )
                copied_files[rel_str] = backup_rel_str
            except Exception as e:
                raise SnapshotError(f"Failed to checkpoint file {file}: {e}")

        logger.info("Created checkpoint %s containing %d files", checkpoint_id, len(files_to_track))
        return {
            "checkpoint_id": checkpoint_id,
            "copied_files": copied_files,
        }

    def restore_checkpoint(self, checkpoint_id: str, checkpoint_data: dict[str, Any]) -> None:
        """Restore workspace files back to their checkpointed state.

        Also cleans up any temporary checkpoint files afterwards.
        """
        checkpoint_path = self.snapshots_dir / checkpoint_id
        copied_files = checkpoint_data.get("copied_files", {})

        for rel_path_str, backup_rel_str in copied_files.items():
            target_file = (self.workspace_path / rel_path_str).resolve()
            try:
                validate_workspace_path(target_file, self.workspace_path, "restore target file")
            except ValueError as e:
                logger.error("Path containment violation in restore target: %s", e)
                raise

            if backup_rel_str is None:
                # File did not exist when checkpoint was created, delete it if it does now
                if target_file.exists():
                    try:
                        if target_file.is_dir():
                            shutil.rmtree(target_file)
                        else:
                            target_file.unlink()
                        logger.debug("Deleted newly created file during rollback: %s", target_file)
                    except Exception as e:
                        raise SnapshotError(
                            f"Failed to remove newly created file {target_file} during rollback: {e}"
                        )
            else:
                # Restore original file content
                backup_file = (self.workspace_path / backup_rel_str).resolve()
                try:
                    validate_workspace_path(backup_file, self.workspace_path, "backup source file")
                except ValueError as e:
                    logger.error("Path containment violation in restore source: %s", e)
                    raise

                if not backup_file.exists():
                    raise SnapshotError(f"Backup file not found: {backup_file}")
                try:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_file, target_file)
                    logger.debug("Restored original file: %s", target_file)
                except Exception as e:
                    raise SnapshotError(f"Failed to restore file {target_file} from backup: {e}")

        # Clean up backup folder
        try:
            if checkpoint_path.exists():
                shutil.rmtree(checkpoint_path)
        except Exception as e:
            logger.warning("Could not clean up snapshot directory %s: %s", checkpoint_path, e)

        logger.info("Successfully restored checkpoint %s", checkpoint_id)
