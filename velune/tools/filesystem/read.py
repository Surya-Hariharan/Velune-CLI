"""Filesystem read tools."""

from pathlib import Path

from velune.permissions.gate import resolve_tool_path
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem.ignore import load_ignore


class ReadFile(BaseTool):
    """Tool for reading file contents."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    def get_name(self) -> str:
        return "read_file"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_description(self) -> str:
        return "Read the contents of a file"

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [path_action(ActionType.READ, args.get("file_path", ""), boundary, "read file")]

    async def execute(self, file_path: str) -> str:
        """Read file contents."""
        path = resolve_tool_path(file_path, self.workspace, label="ReadFile")
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(path, encoding="utf-8") as f:
            return f.read()

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to read",
                }
            },
            "required": ["file_path"],
        }


class ReadDirectory(BaseTool):
    """Tool for reading directory contents."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()
        self._ignore = load_ignore(self.workspace)

    def get_name(self) -> str:
        return "read_directory"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_description(self) -> str:
        return "List the contents of a directory"

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [
            path_action(ActionType.READ, args.get("directory_path", "."), boundary, "list folder")
        ]

    async def execute(self, directory_path: str) -> list[str]:
        """List directory contents, excluding .veluneignore patterns."""
        path = resolve_tool_path(directory_path, self.workspace, label="ReadDirectory")
        if not path.exists() or not path.is_dir():
            raise NotADirectoryError(f"Directory not found: {directory_path}")

        return [item.name for item in path.iterdir() if not self._ignore.is_ignored(item)]

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "directory_path": {
                    "type": "string",
                    "description": "Path to the directory to list",
                }
            },
            "required": ["directory_path"],
        }
