"""Folder and move/rename tools.

Implemented in Python rather than by shelling out, so they work the same on
Windows (where ``mkdir``/``rmdir``/``move`` are cmd.exe builtins the
shell-less sandbox can't run) and on POSIX. Every call is described as
actions and authorized by the permission gate before it runs; paths resolve
against the session boundary (the workspace, plus any location the user
granted for this task). Each change is verified after it is made and recorded
in the turn's undo journal (``/rewind``), see
:mod:`velune.tools.filesystem.mutation`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from velune.permissions.gate import resolve_tool_path
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem import mutation as mut


class _FsTool(BaseTool):
    def __init__(
        self, workspace: Path | None = None, tracker: mut.FileVersionTracker | None = None
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.tracker = tracker or mut.FileVersionTracker()

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    def _display(self, path: Path) -> str:
        return mut.display_path(path, self.workspace)


class CreateDirectory(_FsTool):
    def get_name(self) -> str:
        return "create_directory"

    def get_description(self) -> str:
        return "Create a folder (and any missing parent folders)."

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Folder to create"}},
            "required": ["path"],
        }

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [
            path_action(
                ActionType.CREATE_DIRECTORY, args.get("path", ""), boundary, "create folder"
            )
        ]

    async def execute(self, path: str) -> str:
        target = resolve_tool_path(path, self.workspace, label="create_directory")
        if target.exists() and not target.is_dir():
            raise FileExistsError(f"A file already exists at {target}")
        if target.is_dir():
            return f"Folder already existed: {target}"
        # Record every folder this call creates, outermost first, so /rewind removes them all.
        created: list[Path] = []
        probe = target
        while not probe.exists():
            created.append(probe)
            probe = probe.parent
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise mut.MutationVerificationError(f"{target} is not a folder after creating it")
        for folder in reversed(created):
            mut.journal(mut.DirCreated(folder))
        return f"Created folder {target}"


class DeleteDirectory(_FsTool):
    def get_name(self) -> str:
        return "delete_directory"

    def get_description(self) -> str:
        return (
            "Delete a folder. Set recursive=true to delete a folder that still has "
            "contents (high-risk: always asks for confirmation)."
        )

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to delete"},
                "recursive": {
                    "type": "boolean",
                    "description": "Also delete everything inside the folder (default false)",
                },
            },
            "required": ["path"],
        }

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType, Risk
        from velune.permissions.boundary import path_action

        target = boundary.resolve(args.get("path", ""))
        non_empty = target.is_dir() and any(target.iterdir())
        recursive = bool(args.get("recursive")) and non_empty
        is_root = target == boundary.workspace_root
        risk = Risk.HIGH if (recursive or is_root) else Risk.MEDIUM
        reason = (
            "delete the whole workspace"
            if is_root
            else ("recursive deletion" if recursive else "delete empty folder")
        )
        # The path as requested, not the resolved one: protection judges both identities.
        return [
            path_action(
                ActionType.DELETE_DIRECTORY, args.get("path", ""), boundary, reason, risk=risk
            )
        ]

    async def execute(self, path: str, recursive: bool = False) -> str:
        target = resolve_tool_path(path, self.workspace, label="delete_directory")
        if not target.exists():
            raise FileNotFoundError(f"Folder not found: {target}")
        if not target.is_dir() or mut.is_link(target):
            raise NotADirectoryError(f"Not a folder: {target} (use delete_file)")
        if any(target.iterdir()):
            if not recursive:
                raise OSError(
                    f"Folder is not empty: {target}. Pass recursive=true to delete its contents."
                )
            count = mut.count_entries(target)
            snapshot = mut.snapshot_tree(target)
            shutil.rmtree(target)
            message = f"Deleted folder {target} and {count} item(s) inside it"
        else:
            snapshot = []
            target.rmdir()
            message = f"Deleted empty folder {target}"
        if target.exists():
            raise mut.MutationVerificationError(f"{target} still exists after deleting it")
        mut.journal(mut.DirDeleted(target, snapshot))
        if snapshot is None:
            message += " (too large to keep a copy: /rewind cannot restore it)"
        return message


class MovePath(_FsTool):
    def get_name(self) -> str:
        return "move_path"

    def get_description(self) -> str:
        return "Move or rename a file or folder."

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Existing file or folder"},
                "destination": {"type": "string", "description": "New path"},
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing destination file (default false)",
                },
            },
            "required": ["source", "destination"],
        }

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType, Risk
        from velune.permissions.boundary import path_action, protected_descendant_reason

        raw_source, raw_dest = args.get("source", ""), args.get("destination", "")
        source = boundary.resolve(raw_source)
        dest = boundary.resolve(raw_dest)
        overwrite = bool(args.get("overwrite")) and dest.exists()
        risk = Risk.MEDIUM if overwrite else Risk.LOW
        # A folder carries its contents with it: moving one onto ``.velune`` (or a folder that
        # holds ``.git``/``.velune``/``velune.toml``) installs protected files at the destination
        # without ever naming them. Judge what the move would put there.
        installs = (
            protected_descendant_reason(source, dest, boundary.protected_roots())
            if source.is_dir()
            else ""
        )
        dest_reason = "move destination"
        dest_risk = risk
        if installs:
            dest_risk = Risk.HIGH
            dest_reason = f"move destination: {installs}"
        # The paths are passed as requested (not resolved) so a link's own name is judged too.
        return [
            path_action(
                ActionType.MOVE, raw_source, boundary, "move / rename", risk=risk, detail=str(dest)
            ),
            # The destination is checked too: moving *into* an outside or secret
            # location must be caught just like moving out of one.
            path_action(ActionType.CREATE_FILE, raw_dest, boundary, dest_reason, risk=dest_risk),
        ]

    async def execute(self, source: str, destination: str, overwrite: bool = False) -> str:
        src = resolve_tool_path(source, self.workspace, label="move_path source")
        dst = resolve_tool_path(destination, self.workspace, label="move_path destination")
        if not src.exists():
            raise FileNotFoundError(f"Not found: {src}")
        if src == dst:
            raise ValueError(f"Source and destination are the same: {src}")
        # On a case-insensitive filesystem renaming "Readme.md" to "README.md" finds the
        # destination "already existing" — it is the same file, so it is a plain rename.
        case_rename = dst.exists() and os.path.samefile(src, dst)
        if src.is_dir() and (dst == src or src in dst.parents):
            raise ValueError(f"Cannot move folder {src} into itself ({dst})")

        overwritten: bytes | None = None
        overwritten_mode: int | None = None
        if dst.exists() and not case_rename:
            if not overwrite:
                raise FileExistsError(
                    f"Destination already exists: {dst}. Pass overwrite=true to replace a file."
                )
            if dst.is_dir() or src.is_dir():
                raise FileExistsError(f"Cannot overwrite {dst}: only a file can replace a file")
            overwritten, kept = mut.read_before_image(dst)
            overwritten_mode = mut.file_mode(dst)
            if not kept:
                mut.journal_irreversible(f"overwrote {dst}")

        is_file = src.is_file()
        sha_before = mut.file_sha256(src) if is_file else None
        mut.ensure_parent(dst)
        if is_file or case_rename:
            try:
                # Atomic on one filesystem, including replacing an existing destination file.
                os.replace(src, dst)
            except OSError:
                if case_rename or dst.exists():
                    raise
                shutil.move(str(src), str(dst))  # across filesystems: copy, then delete
        else:
            shutil.move(str(src), str(dst))

        if not dst.exists() or (src.exists() and not case_rename):
            raise mut.MutationVerificationError(f"Move of {src} to {dst} did not complete")
        if sha_before is not None and mut.file_sha256(dst) != sha_before:
            raise mut.MutationVerificationError(f"{dst} does not have {src}'s content after moving")

        if is_file:
            self.tracker.forget(src)
            if sha_before is not None:
                self.tracker.record(dst, sha_before)
        mut.journal(mut.Moved(src, dst, overwritten, sha_before, overwritten_mode))
        return f"Moved {src} → {dst} (verified)"
