"""Gitignore-aware file scanner for workspace discovery."""

import fnmatch
from pathlib import Path

DEFAULT_VELUNEIGNORE = """\
# Velune index exclusions
# Secrets and credentials
.env
.env.*
*.pem
*.key
*.p12
*.pfx
secrets/
credentials/

# Large generated files
*.min.js
*.min.css
dist/
build/
__pycache__/
*.pyc
.mypy_cache/
.ruff_cache/

# Data and media
*.sqlite
*.db
*.csv
*.parquet
data/
datasets/
*.jpg
*.jpeg
*.png
*.gif
*.mp4
*.zip
*.tar.gz

# IDE
.idea/
.vscode/settings.json
*.swp
"""


class FilesystemScanner:
    """Discovers source files inside a workspace, strictly adhering to .gitignore rules."""

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()
        self.gitignore_patterns = self._load_gitignore() + self._load_veluneignore()

    def _load_gitignore(self) -> list[str]:
        """Loads and parses .gitignore rules along with core default exclusions."""
        patterns = [
            # Default exclusions
            ".git",
            ".github",
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            ".venv",
            "venv",
            "env",
            "node_modules",
            "dist",
            "build",
            ".velune",
            "*.egg-info",
            ".pytest_cache",
            ".mypy_cache",
            "CVS",
            ".DS_Store",
        ]

        gitignore_path = self.root_path / ".gitignore"
        if gitignore_path.exists():
            try:
                with open(gitignore_path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            # Normalize trailing slash to match standard glob behavior
                            if line.endswith("/"):
                                line = line[:-1]
                            patterns.append(line)
            except Exception:
                pass

        return patterns

    def _load_veluneignore(self) -> list[str]:
        """Loads and parses .veluneignore rules from the workspace root."""
        patterns: list[str] = []
        veluneignore_path = self.root_path / ".veluneignore"
        if not veluneignore_path.exists():
            return patterns
        try:
            with open(veluneignore_path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if line.endswith("/"):
                            line = line[:-1]
                        patterns.append(line)
        except Exception:
            pass
        return patterns

    def is_ignored(self, path: Path) -> bool:
        """Determines if a path is excluded by .gitignore or default ignore rules."""
        try:
            rel_path = path.resolve().relative_to(self.root_path)
        except ValueError:
            # Not under root
            return True

        path_parts = rel_path.parts
        path_str = str(rel_path).replace("\\", "/")

        for pattern in self.gitignore_patterns:
            # Check direct match or directory component match
            if fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(path_str, f"*/{pattern}"):
                return True
            for part in path_parts:
                if fnmatch.fnmatch(part, pattern):
                    return True

        return False

    def scan(self, extensions: list[str] | None = None) -> list[Path]:
        """Scans the repository recursively and lists all valid files."""
        files: list[Path] = []
        self._recursive_scan(self.root_path, extensions, files)
        return files

    def _recursive_scan(self, current_dir: Path, extensions: list[str] | None, accumulator: list[Path]) -> None:
        """Recurses through directory structure, skipping ignored directories entirely to optimize speed."""
        try:
            for item in current_dir.iterdir():
                if self.is_ignored(item):
                    continue

                if item.is_dir():
                    self._recursive_scan(item, extensions, accumulator)
                elif item.is_file():
                    if extensions is None or item.suffix.lower() in extensions:
                        accumulator.append(item)
        except PermissionError:
            pass  # Fail silently for protected folders

    def scan_code_files(self) -> list[Path]:
        """Convenience method to scan for common source code extensions."""
        code_extensions = [
            ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
            ".c", ".cpp", ".h", ".cs", ".php", ".rb", ".swift", ".kt"
        ]
        return self.scan(code_extensions)
