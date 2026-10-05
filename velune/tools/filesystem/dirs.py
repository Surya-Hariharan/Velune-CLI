"""Folder and move/rename tools.

Implemented in Python rather than by shelling out, so they work the same on
Windows (where ``mkdir``/``rmdir``/``move`` are cmd.exe builtins the
shell-less sandbox can't run) and on POSIX. Every call is described as
actions and authorized by the permission gate before it runs; paths resolve
against the session boundary (the workspace, plus any location the user
granted for this task).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from velune.permissions.gate import resolve_tool_path
from velune.tools.base.tool import BaseTool, ToolPermission


class _FsTool(BaseTool):
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}


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
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        return f"Folder already existed: {target}" if existed else f"Created folder {target}"


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
                    "description": "Also delete everything inside the folder",
                    "default": False,
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
        return [path_action(ActionType.DELETE_DIRECTORY, target, boundary, reason, risk=risk)]

    async def execute(self, path: str, recursive: bool = False) -> str:
        target = resolve_tool_path(path, self.workspace, label="delete_directory")
        if not target.exists():
            raise FileNotFoundError(f"Folder not found: {target}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a folder: {target} (use delete_file)")
        if any(target.iterdir()):
            if not recursive:
                raise OSError(
                    f"Folder is not empty: {target}. Pass recursive=true to delete its contents."
                )
            count = sum(1 for _ in target.rglob("*"))
            shutil.rmtree(target)
            return f"Deleted folder {target} and {count} item(s) inside it"
        target.rmdir()
        return f"Deleted empty folder {target}"


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
                    "description": "Replace an existing destination file",
                    "default": False,
                },
            },
            "required": ["source", "destination"],
        }

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType, Risk
        from velune.permissions.boundary import path_action

        source = boundary.resolve(args.get("source", ""))
        dest = boundary.resolve(args.get("destination", ""))
        overwrite = bool(args.get("overwrite")) and dest.exists()
        risk = Risk.MEDIUM if overwrite else Risk.LOW
        return [
            path_action(
                ActionType.MOVE, source, boundary, "move / rename", risk=risk, detail=str(dest)
            ),
            # The destination is checked too: moving *into* an outside or secret
            # location must be caught just like moving out of one.
            path_action(ActionType.CREATE_FILE, dest, boundary, "move destination", risk=risk),
        ]

    async def execute(self, source: str, destination: str, overwrite: bool = False) -> str:
        src = resolve_tool_path(source, self.workspace, label="move_path source")
        dst = resolve_tool_path(destination, self.workspace, label="move_path destination")
        if not src.exists():
            raise FileNotFoundError(f"Not found: {src}")
        if dst.exists():
            if not overwrite or dst.is_dir():
                raise FileExistsError(f"Destination already exists: {dst}")
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved {src} → {dst}"
