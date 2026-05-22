"""Per-file/module semantic summarization."""

from pathlib import Path
from typing import Optional


class SemanticSummarizer:
    """Summarizes files and modules semantically."""

    def __init__(self):
        pass

    def summarize_file(self, file_path: Path) -> str:
        """Generate a semantic summary of a file."""
        # Simple summary based on file content
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return f"Unable to read {file_path}"
        
        # Extract key information
        lines = content.split("\n")
        
        # Count functions and classes
        functions = [l for l in lines if l.strip().startswith("def ")]
        classes = [l for l in lines if l.strip().startswith("class ")]
        
        summary = f"File: {file_path.name}\n"
        summary += f"Functions: {len(functions)}\n"
        summary += f"Classes: {len(classes)}\n"
        summary += f"Lines: {len(lines)}\n"
        
        return summary

    def summarize_module(self, directory: Path) -> str:
        """Generate a semantic summary of a module."""
        from velune.repository.scanner.filesystem import FilesystemScanner
        
        scanner = FilesystemScanner(directory)
        files = scanner.scan_code_files()
        
        summary = f"Module: {directory.name}\n"
        summary += f"Files: {len(files)}\n"
        
        for file_path in files[:10]:  # Limit to first 10 files
            summary += f"  - {file_path.name}\n"
        
        if len(files) > 10:
            summary += f"  ... and {len(files) - 10} more files\n"
        
        return summary
