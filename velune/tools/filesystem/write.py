"""Filesystem write tools."""

from pathlib import Path

from velune.tools.base.tool import BaseTool
from velune.execution.path_guard import validate_workspace_path


class WriteFile(BaseTool):
    """Tool for writing file contents."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    def get_name(self) -> str:
        return "write_file"

    def get_description(self) -> str:
        return "Write content to a file"

    async def execute(self, file_path: str, content: str) -> str:
        """Write content to file."""
        path = Path(file_path)
        validate_workspace_path(path, self.workspace, label="WriteFile")
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return f"Successfully wrote to {file_path}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        }


class CreateFile(BaseTool):
    """Tool for creating an empty file."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    def get_name(self) -> str:
        return "create_file"

    def get_description(self) -> str:
        return "Create an empty file"

    async def execute(self, file_path: str) -> str:
        """Create an empty file."""
        path = Path(file_path)
        validate_workspace_path(path, self.workspace, label="CreateFile")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

        return f"Successfully created {file_path}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to create",
                },
            },
            "required": ["file_path"],
        }


class DeleteFile(BaseTool):
    """Tool for deleting a file."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    def get_name(self) -> str:
        return "delete_file"

    def get_description(self) -> str:
        return "Delete a file"

    async def execute(self, file_path: str) -> str:
        """Delete a file."""
        path = Path(file_path)
        validate_workspace_path(path, self.workspace, label="DeleteFile")
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        path.unlink()
        return f"Successfully deleted {file_path}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to delete",
                },
            },
            "required": ["file_path"],
        }
