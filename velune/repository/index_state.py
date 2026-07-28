"""Persistent index state for incremental repository indexing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("velune.repository.index_state")

# ---------------------------------------------------------------------------
# Cross-writer mutual exclusion
# ---------------------------------------------------------------------------
#
# Three call sites read-modify-write the same ``index_state.json``:
# ``RepositoryIntelligenceEngine``'s background change-detection loop and
# ``RepositoryCognitionService.run_incremental()`` both go through
# ``IncrementalIndexer.apply_delta`` (async, on the event loop); a full
# ``RepositoryCognitionService.index()`` run goes through
# ``_persist_index_state`` (sync, may run in a worker thread via
# ``asyncio.to_thread`` or directly with no event loop at all). Each of these
# independently loads its own in-memory ``IndexState`` snapshot, mutates it,
# and saves — so far apart in time that ``save()``'s own atomicity (see
# ``IndexState.save``) doesn't help: whichever writer finishes last simply
# overwrites the other's changes wholesale, a lost-update race, not a torn
# read.
#
# A ``threading.Lock`` (rather than ``asyncio.Lock``) is used because the sync
# and async call sites are not guaranteed to run on the same thread. The
# async wrapper below polls a non-blocking acquire with an ``asyncio.sleep``
# between attempts instead of a single blocking acquire, so a coroutine
# waiting for the lock never blocks the event loop thread — a plain blocking
# acquire from a coroutine could deadlock the whole loop if the lock is held
# by another coroutine on that same thread that needs to run to release it.
_state_locks: dict[str, threading.Lock] = {}
_state_locks_guard = threading.Lock()


def _get_state_lock(path: Path) -> threading.Lock:
    """Return the process-wide lock guarding *path*, creating it on first use."""
    key = str(path.resolve()) if path.exists() or path.parent.exists() else str(path)
    with _state_locks_guard:
        lock = _state_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _state_locks[key] = lock
        return lock


@contextlib.contextmanager
def index_state_lock(path: Path):
    """Blocking, synchronous critical-section guard for *path*'s state file."""
    lock = _get_state_lock(path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextlib.asynccontextmanager
async def index_state_lock_async(path: Path, poll_interval: float = 0.02):
    """Async critical-section guard for *path*'s state file.

    Never blocks the event loop thread: each acquire attempt is
    non-blocking, with ``asyncio.sleep`` yielding control between attempts.
    """
    lock = _get_state_lock(path)
    while not lock.acquire(blocking=False):
        await asyncio.sleep(poll_interval)
    try:
        yield
    finally:
        lock.release()


@dataclass
class IndexedFile:
    """Per-file metadata stored between indexing sessions."""

    path: str  # Relative to workspace root (forward-slash)
    content_hash: str  # SHA-256 of file contents at index time
    language: str  # Detected language (e.g. "python")
    symbol_count: int  # Number of parsed symbols (0 if parsing failed)
    indexed_at: float  # Unix timestamp when this entry was written

    # Cheap staleness signals, checked *before* the content hash. Without these
    # the indexer had no way to skip a file short of reading and SHA-256ing it,
    # so every delta computation re-hashed the entire repository — which the
    # change-detection loop triggers every 3 seconds on any dirty working tree.
    # A file whose mtime and size both match is taken as unchanged.
    #
    # Defaulted so an index_state.json written before these existed still loads
    # (it simply has no fast signal and falls back to hashing, once).
    mtime: float = 0.0
    size: int = 0

    def unchanged_on_disk(self, stat: os.stat_result) -> bool:
        """True when mtime *and* size both match — no need to read the file.

        Conservative on purpose: a zeroed mtime/size (an entry from an older
        state file) never matches, so we hash rather than wrongly skip.
        """
        if not self.mtime or not self.size:
            return False
        return self.size == stat.st_size and self.mtime == stat.st_mtime


@dataclass
class IndexState:
    """Full workspace index state persisted between Velune sessions.

    Saved as JSON at ``.velune/index_state.json``.  The ``last_commit_sha``
    is used for the primary fast-path check: if git HEAD has not moved since
    the last index run, file scanning can be skipped entirely.
    """

    workspace_root: str
    last_commit_sha: str | None  # git HEAD SHA at last full index; None if no git
    last_indexed_at: float  # Unix timestamp of the last completed index
    file_index: dict[str, IndexedFile] = field(default_factory=dict)  # rel_path → IndexedFile

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Atomically serialize this state to *path* as JSON.

        Written to a temp file and then ``os.replace``d into position, because
        two indexers write this file (``RepositoryCognitionService`` and
        ``RepositoryIntelligenceEngine``, both against ``.velune/index_state.json``).
        A plain ``open(path, "w")`` truncates first, so a reader arriving mid-write
        saw a partial file, failed to parse it, got ``None`` back — and ``None``
        is indistinguishable from "no state yet", so the caller concluded it was a
        first run and re-indexed the whole repository from scratch.

        ``os.replace`` is atomic on both POSIX and Windows, so a reader now sees
        either the old complete file or the new one, never a torn one.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "workspace_root": self.workspace_root,
            "last_commit_sha": self.last_commit_sha,
            "last_indexed_at": self.last_indexed_at,
            "file_index": {k: asdict(v) for k, v in self.file_index.items()},
        }
        # A distinct temp name per writer: two concurrent saves must not land on
        # the same scratch file and interleave their bytes.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            # No indent= — this is a machine-read cache, and pretty-printing a
            # 10k-file index rewrites multiple megabytes on every save.
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Could not save index state to %s: %s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def load(cls, path: Path) -> IndexState | None:
        """Deserialize from *path*.  Returns ``None`` if the file is missing or corrupt."""
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            file_index = {k: IndexedFile(**v) for k, v in data.get("file_index", {}).items()}
            return cls(
                workspace_root=data["workspace_root"],
                last_commit_sha=data.get("last_commit_sha"),
                last_indexed_at=float(data.get("last_indexed_at", 0.0)),
                file_index=file_index,
            )
        except Exception as exc:
            logger.warning("Could not load index state from %s: %s", path, exc)
            return None

    @classmethod
    def empty(cls, workspace_root: str) -> IndexState:
        """Return a blank state (first-run placeholder)."""
        return cls(
            workspace_root=workspace_root,
            last_commit_sha=None,
            last_indexed_at=0.0,
            file_index={},
        )

    def update_file(self, indexed_file: IndexedFile) -> None:
        self.file_index[indexed_file.path] = indexed_file

    def remove_file(self, path: str) -> None:
        self.file_index.pop(path, None)

    def touch(self, commit_sha: str | None) -> None:
        """Mark the state as freshly indexed at the current time."""
        self.last_commit_sha = commit_sha
        self.last_indexed_at = time.time()
