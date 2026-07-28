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

from velune.repository._native import scan_directory as _native_scan_directory
from velune.repository._native import sha256_file as _native_sha256_file
from velune.repository.index_state import IndexedFile, IndexState, index_state_lock_async
from velune.repository.schemas import MAX_STRUCTURAL_PARSE_BYTES, is_generated_content

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
    # (old_path, new_path) pairs where a to_remove path's stored content
    # hash exactly matches a to_add path's computed hash — very likely the
    # same file moved, not a coincidental delete+add. Purely additive/
    # informational: the paths involved still appear in to_add/to_remove
    # above (so anything that only reads those, e.g. KnowledgeGraphPatcher,
    # is unaffected), but apply_delta uses this to skip re-reading and
    # re-parsing content it already knows hasn't changed. See baseline
    # §3.3/§4.2: "Renames are not detected as renames... anything keyed by
    # the old path ... goes stale until the next successful patch cycle
    # recreates it under the new path."
    renames: list[tuple[str, str]] = field(default_factory=list)

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

        The whole load-mutate-save sequence is serialized against every other
        writer of the same state file (``RepositoryIntelligenceEngine``'s
        background loop, another ``apply_delta`` call, and a full
        ``RepositoryCognitionService.index()`` run all write here) — without
        it, two concurrent writers each load their own stale snapshot and
        whichever saves last silently discards the other's updates entirely,
        not just the conflicting entries.
        """
        async with index_state_lock_async(self.state_path):
            state = IndexState.load(self.state_path) or IndexState.empty(str(self.workspace_root))

            # Capture rename sources' old entries before the removal loop
            # below pops them, so the fast path in the parse loop can reuse
            # their already-known content_hash/language/symbol_count instead
            # of re-reading and re-parsing content that provably hasn't
            # changed (that's how the rename was detected in the first
            # place — see IncrementalIndexer._detect_renames).
            rename_source: dict[str, IndexedFile] = {}
            for old_path, new_path in delta.renames:
                old_entry = state.file_index.get(old_path)
                if old_entry is not None:
                    rename_source[new_path] = old_entry

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
                old_entry = rename_source.get(rel_path)
                try:
                    if old_entry is not None:
                        entry = await asyncio.to_thread(
                            self._reindex_renamed, rel_path, full_path, old_entry, now
                        )
                    else:
                        # One hop to a worker thread for the whole file: stat + read +
                        # SHA-256 + parse. Previously only the parse was off-loaded, so
                        # the read and the full hash of every changed file ran on the
                        # event loop and stalled the REPL in proportion to file size.
                        entry = await asyncio.to_thread(self._index_one, rel_path, full_path, now)
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.debug("Skipped %s during apply_delta: %s", rel_path, exc)
                else:
                    state.update_file(entry)
                    logger.debug("Indexed: %s (%d symbols)", rel_path, entry.symbol_count)
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

    def _index_one(self, rel_path: str, full_path: Path, now: float) -> IndexedFile:
        """Stat, read, hash and parse one file. Runs entirely off the event loop.

        Raises ``FileNotFoundError`` if the file vanished between delta
        computation and here — a normal race when the user is editing.

        Files over ``MAX_STRUCTURAL_PARSE_BYTES`` are hashed (for staleness
        tracking) but never read for structural parsing — the same opaque-
        file guard ``RepositoryIndexer.index()`` applies on a full run, kept
        consistent here so an incremental update can't reintroduce the one
        oversized file dominating cost that the full-run guard prevents.
        """
        stat = full_path.stat()  # raises FileNotFoundError if it's gone
        sha = self._hash_file(full_path)

        if stat.st_size > MAX_STRUCTURAL_PARSE_BYTES:
            from velune.repository.parser import RepositorySnapshotParser

            language = RepositorySnapshotParser()._detect_language(full_path).value
            return IndexedFile(
                path=rel_path,
                content_hash=sha,
                language=language,
                symbol_count=0,
                indexed_at=now,
                mtime=stat.st_mtime,
                size=stat.st_size,
            )

        content = full_path.read_text(encoding="utf-8", errors="ignore")

        if is_generated_content(content):
            from velune.repository.parser import RepositorySnapshotParser

            language = RepositorySnapshotParser()._detect_language(full_path).value
            return IndexedFile(
                path=rel_path,
                content_hash=sha,
                language=language,
                symbol_count=0,
                indexed_at=now,
                mtime=stat.st_mtime,
                size=stat.st_size,
            )

        symbols, language = self._parse_file(full_path, content)
        return IndexedFile(
            path=rel_path,
            content_hash=sha,
            language=language,
            symbol_count=len(symbols),
            indexed_at=now,
            mtime=stat.st_mtime,
            size=stat.st_size,
        )

    def _reindex_renamed(
        self, rel_path: str, full_path: Path, old_entry: IndexedFile, now: float
    ) -> IndexedFile:
        """Build a renamed file's entry from its old one, without re-reading content.

        Only reached when ``_detect_renames`` has already confirmed the
        content hash is unchanged, so there is nothing new to read, hash,
        or parse — only the path/mtime/size need refreshing to reflect the
        move.
        """
        stat = full_path.stat()  # raises FileNotFoundError if it's gone
        return IndexedFile(
            path=rel_path,
            content_hash=old_entry.content_hash,
            language=old_entry.language,
            symbol_count=old_entry.symbol_count,
            indexed_at=now,
            mtime=stat.st_mtime,
            size=stat.st_size,
        )

    # Public aliases. RepositoryCognitionService needs both of these, and was
    # reaching into the private names from another module to get them — every
    # call site carrying a `# type: ignore[attr-defined]` to silence the fallout.
    def git_sha(self) -> str | None:
        """The current git HEAD SHA, or None outside a git repo."""
        return self._get_git_sha()

    def working_tree_is_clean(self) -> bool:
        """True when the tree has no modifications and no untracked files."""
        return self._working_tree_is_clean()

    def _get_git_sha(self) -> str | None:
        """Return the current git HEAD SHA, or None if git is unavailable."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                timeout=5,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _working_tree_is_clean(self) -> bool:
        """Return True when the tree has no modifications *and* no new files.

        Uses ``git status --porcelain``, not ``git diff HEAD``. ``git diff`` does
        not report untracked files, so with it a brand-new file that had never
        been ``git add``ed made the tree look clean — the caller took the fast
        path, returned an empty delta without touching the disk, and the new file
        stayed invisible to the index until HEAD moved or some *tracked* file
        happened to change. New code was simply never indexed.
        """
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                timeout=5,
                encoding="utf-8",
                errors="replace",
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
        add_hashes: dict[str, str] = {}  # to_add's rel_path -> content hash

        for rel_path, abs_path in current.items():
            stored_entry = stored.get(rel_path)

            # Cheap check first. A stat() is orders of magnitude cheaper than
            # reading and SHA-256ing the file, and this path runs on every
            # change-detection tick (every 3s while the tree is dirty, which is
            # to say: throughout normal development). Hashing the whole repo each
            # time was pure waste — the hash is only needed to catch a file whose
            # size and mtime are unchanged but whose bytes are not.
            if stored_entry is not None:
                try:
                    if stored_entry.unchanged_on_disk(abs_path.stat()):
                        continue
                except OSError:
                    continue

            try:
                sha = self._hash_file(abs_path)
            except Exception:
                continue

            if stored_entry is None:
                to_add.append(rel_path)
                add_hashes[rel_path] = sha
            elif stored_entry.content_hash != sha:
                to_update.append(rel_path)

        to_remove = [p for p in stored if p not in current]
        renames = self._detect_renames(to_remove, add_hashes, stored)

        return IndexDelta(to_add=to_add, to_update=to_update, to_remove=to_remove, renames=renames)

    @staticmethod
    def _detect_renames(
        to_remove: list[str], add_hashes: dict[str, str], stored: dict[str, IndexedFile]
    ) -> list[tuple[str, str]]:
        """Pair up removed/added paths whose content hash matches exactly.

        A deliberately conservative heuristic: exact content-hash equality,
        not a similarity score, and each candidate claimed at most once
        (sorted order, first-unclaimed-match) rather than trying to
        disambiguate duplicate-content ties cleverly. An unmatched pair
        simply falls through as an ordinary delete + add — the prior,
        already-correct behavior — so there is no failure mode where
        guessing wrong makes things worse than not guessing at all.
        """
        if not to_remove or not add_hashes:
            return []

        renames: list[tuple[str, str]] = []
        claimed_adds: set[str] = set()
        sorted_adds = sorted(add_hashes)

        for old_path in sorted(to_remove):
            old_entry = stored.get(old_path)
            old_hash = old_entry.content_hash if old_entry else None
            if not old_hash:
                continue
            for new_path in sorted_adds:
                if new_path in claimed_adds:
                    continue
                if add_hashes[new_path] == old_hash:
                    renames.append((old_path, new_path))
                    claimed_adds.add(new_path)
                    break

        return renames

    def _fallback_scan(self) -> list[Path]:
        """Minimal walk used when FilesystemScanner is unavailable.

        Delegates the directory walk to the native extension (falls back to
        pure Python automatically when the Rust wheel isn't installed — see
        velune/repository/_native.py). ``scan_directory`` only prunes by exact
        directory name, so ``_is_always_skip``'s suffix rules (``.egg-info``)
        are still applied as a post-filter to keep results identical to the
        previous pure-Python walk.
        """
        raw_paths = _native_scan_directory(
            str(self.workspace_root), list(_CODE_EXTENSIONS), list(_ALWAYS_SKIP_DIRS)
        )
        results: list[Path] = []
        for raw in raw_paths:
            path = Path(raw)
            if any(_is_always_skip(part) for part in path.parts):
                continue
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
        """SHA-256 hex digest of a file's raw bytes."""
        return _native_sha256_file(path)

    @staticmethod
    def _hash_content(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
