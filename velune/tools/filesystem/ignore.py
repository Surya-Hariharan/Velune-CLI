""".veluneignore — gitignore-syntax file exclusion for Velune file tools.

Ported from Gemini CLI's .geminiignore concept, using Python's `pathspec`
library (already a Velune dependency) for full gitignore-pattern support.

Usage::

    from velune.tools.filesystem.ignore import load_ignore

    spec = load_ignore(workspace)          # loads .veluneignore + defaults
    filtered = spec.filter(all_paths)      # drop ignored paths
    if spec.is_ignored(some_path):
        ...

The default excludes cover VCS dirs, build artifacts, secrets, media blobs,
and OS/IDE noise — matching typical .gitignore convention.  A workspace-level
`.veluneignore` file can extend or override these with any gitignore patterns.
"""

from __future__ import annotations

from pathlib import Path

import pathspec

# Patterns always applied, even without a .veluneignore file.
_DEFAULT_PATTERNS: list[str] = [
    # VCS
    ".git/",
    ".hg/",
    ".svn/",
    ".fossil/",
    # Python caches & build
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.egg-info/",
    "*.egg",
    "dist/",
    "build/",
    ".tox/",
    ".nox/",
    ".venv/",
    "venv/",
    "env/",
    ".env/",
    # Ruff / mypy / pytest caches
    ".ruff_cache/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".dmypy.json",
    # Node
    "node_modules/",
    ".npm/",
    ".yarn/",
    ".pnp.cjs",
    ".pnp.js",
    # JS build outputs
    ".next/",
    ".nuxt/",
    ".output/",
    "out/",
    ".turbo/",
    # Coverage
    "coverage/",
    ".coverage",
    "htmlcov/",
    "*.lcov",
    # Secrets / credentials — NEVER expose these to the model
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.cert",
    "*.crt",
    "*.pfx",
    "*.p12",
    "secrets.json",
    "credentials.json",
    ".secrets/",
    # IDE / OS noise
    ".DS_Store",
    "Thumbs.db",
    ".idea/",
    ".vscode/",
    "*.swp",
    "*.swo",
    "*~",
    # Large media (not useful as LLM context)
    "*.mp4",
    "*.mov",
    "*.avi",
    "*.mkv",
    "*.webm",
    "*.jpg",
    "*.jpeg",
    "*.png",
    "*.gif",
    "*.bmp",
    "*.ico",
    "*.webp",
    "*.tiff",
    "*.svg",
    # Archives
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.tar.bz2",
    "*.tbz2",
    "*.tar.xz",
    "*.gz",
    "*.bz2",
    "*.7z",
    "*.rar",
    "*.xz",
    # Compiled / binary
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.wasm",
    "*.bin",
    "*.class",
    # Databases
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.db-shm",
    "*.db-wal",
    # Velune internals
    ".velune/temp/",
]


class VeluneIgnore:
    """Compiled ignore spec for one workspace root."""

    def __init__(self, spec: pathspec.PathSpec, root: Path) -> None:
        self._spec = spec
        self._root = root.resolve()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_ignored(self, path: Path) -> bool:
        """Return True when *path* should be excluded from file operations."""
        try:
            rel = path.resolve().relative_to(self._root)
        except ValueError:
            # Path is outside the workspace — treat as non-ignored so callers
            # can decide what to do with out-of-tree paths.
            return False
        return self._spec.match_file(rel.as_posix())

    def filter(self, paths: list[Path]) -> list[Path]:
        """Return the subset of *paths* that are NOT ignored."""
        return [p for p in paths if not self.is_ignored(p)]

    def filter_strings(self, paths: list[str]) -> list[str]:
        """String-path variant of :meth:`filter`."""
        return [p for p in paths if not self.is_ignored(Path(p))]

    @property
    def root(self) -> Path:
        return self._root


def load_ignore(workspace: Path) -> VeluneIgnore:
    """Build a :class:`VeluneIgnore` for *workspace*.

    Reads ``.veluneignore`` from the workspace root (if present) and merges
    its patterns on top of :data:`_DEFAULT_PATTERNS`.  Comments (lines
    starting with ``#``) and blank lines are silently skipped.

    Never raises — any file-read error is logged and the default spec is
    returned unchanged.
    """
    patterns: list[str] = list(_DEFAULT_PATTERNS)

    ignore_file = workspace / ".veluneignore"
    if ignore_file.is_file():
        try:
            for line in ignore_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    patterns.append(stripped)
        except OSError:
            pass  # non-fatal: fall back to defaults

    spec = pathspec.PathSpec.from_lines("gitignore", patterns)
    return VeluneIgnore(spec, workspace)


def default_ignore(workspace: Path) -> VeluneIgnore:
    """Return a VeluneIgnore built only from the default patterns (no file)."""
    spec = pathspec.PathSpec.from_lines("gitignore", _DEFAULT_PATTERNS)
    return VeluneIgnore(spec, workspace)
