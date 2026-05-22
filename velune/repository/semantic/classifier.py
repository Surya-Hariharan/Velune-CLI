"""File role classification."""

from pathlib import Path
from enum import Enum


class FileRole(str, Enum):
    """File role classifications."""
    SOURCE = "source"
    TEST = "test"
    CONFIG = "config"
    DOCUMENTATION = "documentation"
    RESOURCE = "resource"
    UNKNOWN = "unknown"


class FileClassifier:
    """Classifies files by their role."""

    def classify(self, file_path: Path) -> FileRole:
        """Classify a file's role."""
        name = file_path.name.lower()
        parent = file_path.parent.name.lower()
        
        # Test files
        if "test" in name or "test" in parent or name.startswith("test_"):
            return FileRole.TEST
        
        # Configuration files
        if name in ["setup.py", "pyproject.toml", "package.json", "tsconfig.json", "cargo.toml"]:
            return FileRole.CONFIG
        
        # Documentation
        if name.endswith(".md") or name.endswith(".rst") or "doc" in parent:
            return FileRole.DOCUMENTATION
        
        # Resources
        if file_path.suffix in [".json", ".yaml", ".yml", ".xml", ".txt"]:
            return FileRole.RESOURCE
        
        # Source files
        if file_path.suffix in [".py", ".js", ".ts", ".rs", ".go", ".java", ".c", ".cpp"]:
            return FileRole.SOURCE
        
        return FileRole.UNKNOWN
