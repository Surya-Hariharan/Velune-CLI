"""Incremental repository indexer that skips unchanged files between sessions.

Algorithm
---------
1. Load the stored ``IndexState`` (or treat as empty on first run).
2. Fetch the current git HEAD SHA.
3. **Fast path**: if HEAD SHA equals the stored SHA *and* the working tree is
   clean (no uncommitted changes), return an empty ``IndexDelta`` immediately —
   no file I/O required.
4. **Slow path**: walk workspace source files, compute SHA-256 for each, compare
   to the stored hashes, and build the delta (added / updated / removed).
5. ``apply_delta`` parses only the files in the delta, updates ``IndexState``,
   and saves it to disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from velune.repository.index_state import IndexedFile, IndexState

logger = logging.getLogger("velune.repository.incremental_indexer")

# Directory names always excluded from the fallback walk.
# These are EXACT directory names (no globs) for efficient set lookup.
_ALWAYS_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".env",
        ".velune",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        # JS/TS build outputs
        ".next",
        ".nuxt",
        ".output",
        ".turbo",
        "out",
        # Coverage
        "coverage",
        "htmlcov",
        # Common build dirs
        "dist",
        "build",
    }
)

# Directory suffixes/patterns handled separately (fnmatch-style, applied to dir name only)
_ALWAYS_SKIP_SUFFIXES = (".egg-info", ".egg")


def _is_always_skip(dir_name: str) -> bool:
    """Return True when a directory should always be excluded from indexing."""
    return dir_name in _ALWAYS_SKIP_DIRS or any(dir_name.endswith(s) for s in _ALWAYS_SKIP_SUFFIXES)


# Import the canonical extension set from scanner to keep them in sync.
# Fall back to a minimal set if the import fails (e.g. during bootstrap).
try:
    from velune.repository.scanner import CODE_EXTENSIONS as _CODE_EXTENSIONS
except Exception:
    _CODE_EXTENSIONS = frozenset(
        {
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
            ".vue",
            ".svelte",
            ".html",
            ".sql",
            ".graphql",
            ".gql",
            ".prisma",
        }
    )

_CODE_EXTENSIONS = _CODE_EXTENSIONS  # re-export for backwards compat

# Backwards-compat alias: external callers (e.g. observability/context_report.py)
# imported _ALWAYS_SKIP before the rename.  Expose the merged set so those
# callers still work without modification.
_ALWAYS_SKIP: frozenset[str] = _ALWAYS_SKIP_DIRS


@dataclass
class IndexDelta:
    """Describes which files need to be added, re-parsed, or removed."""

    to_add: list[str] = field(default_factory=list)  # new files not in stored state
    to_update: list[str] = field(default_factory=list)  # files whose hash changed
    to_remove: list[str] = field(default_factory=list)  # files deleted from disk

    @property
    def is_empty(self) -> bool:
        return not self.to_add and not self.to_update and not self.to_remove

    @property
    def total(self) -> int:
        return len(self.to_add) + len(self.to_update) + len(self.to_remove)


class IncrementalIndexer:
    """Computes and applies file-level deltas to keep ``IndexState`` current."""

    def __init__(self, workspace_root: Path, state_path: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.state_path = state_path
        # Optional progress hook: called with (processed: int, total: int, rel_path: str)
        # after each file is parsed. Assign before calling apply_delta().
        self.progress_callback: Callable[[int, int, str], None] | None = None

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def compute_delta(self) -> IndexDelta:
        """Compare disk state to the stored ``IndexState`` and return the diff.

        The git HEAD SHA is checked first.  If it matches the stored SHA *and*
        the working tree has no uncommitted changes, an empty delta is returned
        immediately — no file reads required.
        """
        state = IndexState.load(self.state_path)
        git_sha = await asyncio.to_thread(self._get_git_sha)

        # --- Fast path ---
        if (
            state is not None
            and state.last_commit_sha is not None
            and state.last_commit_sha == git_sha
        ):
            clean = await asyncio.to_thread(self._working_tree_is_clean)
            if clean:
                logger.debug("Fast path: git SHA matches and working tree is clean.")
                return IndexDelta()

        # --- Slow path: walk files and compare hashes ---
        return await asyncio.to_thread(self._compute_file_delta, state)

    async def apply_delta(self, delta: IndexDelta) -> IndexState:
        """Parse only the files in *delta*, update stored ``IndexState``, save to disk.

        Files in ``delta.to_remove`` are dropped from the state.
        Files in ``delta.to_add`` and ``delta.to_update`` are hashed and parsed.
        """
        state = IndexState.load(self.state_path) or IndexState.empty(str(self.workspace_root))

        # Remove deleted files
        for rel_path in delta.to_remove:
            state.remove_file(rel_path)
            logger.debug("Removed from index: %s", rel_path)

        # Parse added and modified files
        now = time.time()
        _to_process = delta.to_add + delta.to_update
        _total = len(_to_process)
        for _idx, rel_path in enumerate(_to_process):
            full_path = self.workspace_root / rel_path
            if not full_path.exists():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
                sha = self._hash_file(full_path)
                symbols, language = await asyncio.to_thread(self._parse_file, full_path, content)
                state.update_file(
                    IndexedFile(
                        path=rel_path,
                        content_hash=sha,
                        language=language,
                        symbol_count=len(symbols),
                        indexed_at=now,
                    )
                )
                logger.debug("Indexed: %s (%d symbols)", rel_path, len(symbols))
            except Exception as exc:
                logger.debug("Skipped %s during apply_delta: %s", rel_path, exc)
            finally:
                if self.progress_callback is not None:
                    self.progress_callback(_idx + 1, _total, rel_path)

        # Update metadata and persist
        git_sha = await asyncio.to_thread(self._get_git_sha)
        state.touch(git_sha)
        state.workspace_root = str(self.workspace_root)
        state.save(self.state_path)
        return state

    # ------------------------------------------------------------------
    # Synchronous helpers (run in thread pool)
    # ------------------------------------------------------------------

    def _get_git_sha(self) -> str | None:
        """Return the current git HEAD SHA, or None if git is unavailable."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _working_tree_is_clean(self) -> bool:
        """Return True when there are no staged or unstaged modifications."""
        try:
            result = subprocess.run(
                ["git", "diff", "HEAD", "--name-only"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip() == ""
        except Exception:
            pass
        # If git is unavailable, assume dirty so we do the file scan
        return False

    def _compute_file_delta(self, state: IndexState | None) -> IndexDelta:
        """Walk workspace files, compare hashes, and build the delta."""
        stored = state.file_index if state else {}

        # Discover current source files (reuse scanner for .veluneignore support)
        try:
            from velune.repository.scanner import FilesystemScanner

            scanner = FilesystemScanner(self.workspace_root)
            current_paths = scanner.scan_code_files()
        except Exception:
            current_paths = list(self._fallback_scan())

        current: dict[str, Path] = {}
        for p in current_paths:
            try:
                rel = str(p.relative_to(self.workspace_root)).replace("\\", "/")
                current[rel] = p
            except ValueError:
                continue

        to_add: list[str] = []
        to_update: list[str] = []

        for rel_path, abs_path in current.items():
            try:
                sha = self._hash_file(abs_path)
            except Exception:
                continue

            stored_entry = stored.get(rel_path)
            if stored_entry is None:
                to_add.append(rel_path)
            elif stored_entry.content_hash != sha:
                to_update.append(rel_path)

        to_remove = [p for p in stored if p not in current]

        return IndexDelta(to_add=to_add, to_update=to_update, to_remove=to_remove)

    def _fallback_scan(self) -> list[Path]:
        """Minimal walk used when FilesystemScanner is unavailable."""
        results: list[Path] = []
        for path in self.workspace_root.rglob("*"):
            if any(_is_always_skip(part) for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in _CODE_EXTENSIONS:
                results.append(path)
        return results

    def _parse_file(self, full_path: Path, content: str) -> tuple[list, str]:
        """Parse *full_path* and return (symbols, language_str)."""
        try:
            from velune.repository.parser import RepositorySnapshotParser

            parser = RepositorySnapshotParser()
            symbols, _ = parser.parse(full_path, content)
            lang = parser._detect_language(full_path)
            return symbols, lang.value
        except Exception:
            return [], "unknown"

    @staticmethod
    def _hash_file(path: Path) -> str:
        """SHA-256 of a file's raw bytes, read in 8-KiB chunks."""
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()

    @staticmethod
    def _hash_content(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
