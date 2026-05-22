"""Gitignore-aware file discovery."""

from pathlib import Path
from typing import list
import fnmatch
import os


class FilesystemScanner:
    """Scanner for discovering files in a repository."""

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.gitignore_patterns = self._load_gitignore()

    def _load_gitignore(self) -> list[str]:
        """Load .gitignore patterns."""
        gitignore_path = self.root_path / ".gitignore"
        patterns = []
        
        if gitignore_path.exists():
            with open(gitignore_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
        
        # Add default ignore patterns
        patterns.extend([".git", "__pycache__", "*.pyc", ".velune", "node_modules"])
        
        return patterns

    def is_ignored(self, path: Path) -> bool:
        """Check if a path should be ignored."""
        relative_path = path.relative_to(self.root_path)
        path_str = str(relative_path).replace("\\", "/")
        
        for pattern in self.gitignore_patterns:
            if fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(path_str, f"*/{pattern}"):
                return True
        
        return False

    def scan(self, extensions: Optional[list[str]] = None) -> list[Path]:
        """Scan the repository for files."""
        files = []
        
        for root, dirs, filenames in os.walk(self.root_path):
            # Filter ignored directories
            dirs[:] = [d for d in dirs if not self.is_ignored(Path(root) / d)]
            
            for filename in filenames:
                file_path = Path(root) / filename
                
                if self.is_ignored(file_path):
                    continue
                
                if extensions is None or file_path.suffix in extensions:
                    files.append(file_path)
        
        return files

    def scan_code_files(self) -> list[Path]:
        """Scan for code files only."""
        code_extensions = [
            ".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".java", ".c", ".cpp", ".h",
            ".cs", ".php", ".rb", ".swift", ".kt", ".scala", ".dart", ".lua", ".r",
        ]
        return self.scan(code_extensions)
