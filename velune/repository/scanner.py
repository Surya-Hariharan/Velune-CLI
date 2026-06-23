"""Gitignore-aware file scanner for workspace discovery.

Previously used broken fnmatch-based pattern matching that:
  - Did not handle ** glob patterns
  - Did not handle leading-/ (root-anchored) gitignore patterns
  - Did not handle negation patterns (!pattern)
  - Incorrectly matched patterns against individual path components causing
    false-positives (e.g. a pattern 'test' would exclude any path that has
    a component named 'test', even if the user put 'test' in .gitignore to
    exclude a specific top-level file)

Now uses pathspec (already a Velune dependency) which implements the full
gitignore specification including **, negation, and directory-only patterns.

Both .gitignore (at workspace root) and .veluneignore are read.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("velune.repository.scanner")

# -------------------------------------------------------------------------
# Hard-coded always-skip patterns — applied regardless of .gitignore.
# These are patterns so universally unwanted that we bake them in.
# -------------------------------------------------------------------------
_HARDCODED_PATTERNS: list[str] = [
    # VCS internals
    ".git/",
    ".hg/",
    ".svn/",
    ".fossil/",
    # Python compiled / build
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.egg-info/",
    "*.egg",
    # Virtual environments (very common names)
    ".venv/",
    "venv/",
    "env/",
    ".env/",
    # Node
    "node_modules/",
    ".npm/",
    ".yarn/",
    # JS/TS build outputs
    ".next/",
    ".nuxt/",
    ".output/",
    ".turbo/",
    # Tool caches
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    ".tox/",
    ".nox/",
    # Velune internals
    ".velune/",
    # Coverage
    "htmlcov/",
    "*.lcov",
    # IDE / OS noise
    ".DS_Store",
    "Thumbs.db",
    ".idea/",
    # Minified/generated assets (not useful as context)
    "*.min.js",
    "*.min.css",
    # Binary / large media
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.wasm",
    "*.class",
    "*.jpg",
    "*.jpeg",
    "*.png",
    "*.gif",
    "*.bmp",
    "*.ico",
    "*.webp",
    "*.mp4",
    "*.mov",
    "*.avi",
    "*.zip",
    "*.tar.gz",
    "*.tar",
    "*.gz",
    "*.7z",
    "*.rar",
    # Databases / data dumps
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.parquet",
]

# Source code extensions Velune indexes for symbol/route extraction.
# Kept here as the canonical list; incremental_indexer imports this.
CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Core languages
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".cs",
        ".php",
        ".rb",
        ".swift",
        ".kt",
        # Frontend frameworks
        ".vue",
        ".svelte",
        # Templates / markup
        ".html",
        ".htm",
        ".jinja",
        ".jinja2",
        ".j2",
        # Query languages
        ".sql",
        ".graphql",
        ".gql",
        # Schema / config files that are code
        ".prisma",
        ".proto",
        # Mobile
        ".dart",
        ".m",  # Objective-C
        ".mm",  # Objective-C++
        # Other compiled languages
        ".scala",
        ".clj",
        ".cljs",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".ml",
        ".fs",
        ".fsi",
        ".fsx",
        ".lua",
        ".r",
        ".R",
        ".jl",
        # Shell scripts (often contain important wiring logic)
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
    }
)


def unsafe_index_root_reason(root: Path) -> str | None:
    """Return a human-readable reason if *root* must never be recursively indexed.

    Auto-indexing the user's home directory or a filesystem/drive root walks and
    hashes an unbounded tree (OneDrive, Documents, AppData, …), which can stall
    startup indefinitely. The REPL should still launch in such directories — it
    just must not crawl them. Returns ``None`` for ordinary project directories.
    """
    try:
        resolved = root.resolve()
    except Exception:
        return None
    try:
        if resolved == Path.home().resolve():
            return "your home directory"
    except Exception:
        pass
    if resolved.parent == resolved:  # drive/filesystem root: C:\, D:\, /
        return "a filesystem root"
    return None


class FilesystemScanner:
    """Discovers source files inside a workspace, respecting gitignore rules.

    Uses ``pathspec`` for full gitignore-spec compliance:
      - Supports ``**`` double-star patterns
      - Supports root-anchored patterns (``/dist``)
      - Supports negation (``!important.py``)
      - Supports directory-only patterns (``dist/``)

    Pattern priority (later entries can override earlier via negation):
      1. Hard-coded always-skip patterns (VCS dirs, compiled files, etc.)
      2. Workspace ``.gitignore``
      3. Workspace ``.veluneignore`` (highest priority — user overrides)
    """

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()
        self._spec = self._build_spec()

    # ------------------------------------------------------------------
    # Public API (unchanged signature for backward compat)
    # ------------------------------------------------------------------

    def is_ignored(self, path: Path) -> bool:
        """Return True when *path* should be excluded from indexing."""
        try:
            rel = path.resolve().relative_to(self.root_path)
        except ValueError:
            return True  # outside workspace root → skip

        rel_posix = rel.as_posix()

        # pathspec matches directories more reliably with a trailing slash
        if path.is_dir():
            return self._spec.match_file(rel_posix + "/") or self._spec.match_file(rel_posix)

        return self._spec.match_file(rel_posix)

    def scan(self, extensions: list[str] | None = None) -> list[Path]:
        """Scan the workspace recursively and return all valid files."""
        files: list[Path] = []
        self._recursive_scan(self.root_path, extensions, files)
        return files

    def scan_code_files(self) -> list[Path]:
        """Scan for all recognised source-code extensions."""
        return self.scan(list(CODE_EXTENSIONS))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_spec(self):
        """Build a combined pathspec from hardcoded patterns + .gitignore + .veluneignore."""
        try:
            import pathspec  # already a Velune dependency
        except ImportError:
            logger.warning(
                "pathspec not available — falling back to basic ignore rules. "
                "Install it with: pip install pathspec"
            )
            return _FallbackSpec(self.root_path)

        patterns: list[str] = list(_HARDCODED_PATTERNS)

        # Layer 2: workspace .gitignore
        patterns.extend(self._read_ignore_file(self.root_path / ".gitignore"))

        # Layer 3: workspace .veluneignore (highest priority)
        patterns.extend(self._read_ignore_file(self.root_path / ".veluneignore"))

        # Also read nested .gitignore files (important for monorepos)
        for nested in self._find_nested_gitignores():
            try:
                rel_dir = nested.parent.relative_to(self.root_path).as_posix()
                for line in self._read_ignore_file(nested):
                    if line.startswith("!"):
                        # Negation: prefix to scope it
                        patterns.append(f"!{rel_dir}/{line[1:]}")
                    elif line.startswith("/"):
                        # Root-anchored relative to the sub-dir
                        patterns.append(f"{rel_dir}{line}")
                    else:
                        patterns.append(f"{rel_dir}/{line}")
            except Exception:
                pass

        return pathspec.PathSpec.from_lines("gitignore", patterns)

    def _read_ignore_file(self, path: Path) -> list[str]:
        """Read an ignore file and return non-comment, non-empty lines."""
        if not path.is_file():
            return []
        try:
            lines = []
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = raw.strip()
                if stripped and not stripped.startswith("#"):
                    lines.append(stripped)
            return lines
        except Exception:
            return []

    def _find_nested_gitignores(self) -> list[Path]:
        """Find .gitignore files in subdirectories (max 4 levels deep)."""
        found: list[Path] = []
        try:
            for gitignore in self.root_path.rglob(".gitignore"):
                if gitignore == self.root_path / ".gitignore":
                    continue  # already loaded
                # Don't descend into dirs we'd skip anyway
                rel = gitignore.relative_to(self.root_path)
                parts = rel.parts
                if any(
                    p.startswith(".") or p in {"node_modules", "__pycache__", "venv", ".venv"}
                    for p in parts[:-1]
                ):
                    continue
                if len(parts) <= 5:  # cap depth
                    found.append(gitignore)
        except Exception:
            pass
        return found

    def _recursive_scan(
        self, current_dir: Path, extensions: list[str] | None, accumulator: list[Path]
    ) -> None:
        """Recurse through directories, skipping ignored items."""
        try:
            for item in sorted(current_dir.iterdir()):  # sorted for deterministic order
                if self.is_ignored(item):
                    continue
                if item.is_dir():
                    self._recursive_scan(item, extensions, accumulator)
                elif item.is_file():
                    if extensions is None or item.suffix.lower() in extensions:
                        accumulator.append(item)
        except PermissionError:
            pass


class _FallbackSpec:
    """Minimal fallback when pathspec is not installed."""

    def __init__(self, root: Path) -> None:
        self._skip_names = {
            ".git",
            "__pycache__",
            "node_modules",
            ".venv",
            "venv",
            "dist",
            "build",
            ".velune",
            ".next",
            ".nuxt",
        }

    def match_file(self, path: str) -> bool:
        parts = path.replace("\\", "/").rstrip("/").split("/")
        return any(
            p in self._skip_names or p.endswith(".pyc") or p.endswith(".egg-info") for p in parts
        )


# ---------------------------------------------------------------------------
# Backwards-compat alias: DEFAULT_VELUNEIGNORE was a doc-only constant —
# kept here so any external code that imported it still compiles.
# ---------------------------------------------------------------------------
DEFAULT_VELUNEIGNORE = "\n".join(_HARDCODED_PATTERNS)
