"""Filesystem read tools."""

from pathlib import Path

from velune.tools.base.tool import BaseTool


class ReadFile(BaseTool):
    """Tool for reading file contents."""

    def get_name(self) -> str:
        return "read_file"

    def get_description(self) -> str:
        return "Read the contents of a file"

    async def execute(self, file_path: str) -> str:
        """Read file contents."""
        path = Path(file_path)
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

    def get_name(self) -> str:
        return "read_directory"

    def get_description(self) -> str:
        return "List the contents of a directory"

    async def execute(self, directory_path: str) -> list[str]:
        """List directory contents."""
        path = Path(directory_path)
        if not path.exists() or not path.is_dir():
            raise NotADirectoryError(f"Directory not found: {directory_path}")

        return [item.name for item in path.iterdir()]

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
