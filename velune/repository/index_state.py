"""Persistent index state for incremental repository indexing."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("velune.repository.index_state")


@dataclass
class IndexedFile:
    """Per-file metadata stored between indexing sessions."""

    path: str  # Relative to workspace root (forward-slash)
    content_hash: str  # SHA-256 of file contents at index time
    language: str  # Detected language (e.g. "python")
    symbol_count: int  # Number of parsed symbols (0 if parsing failed)
    indexed_at: float  # Unix timestamp when this entry was written


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
        """Serialize this state to *path* as JSON (creates parent dirs as needed)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "workspace_root": self.workspace_root,
            "last_commit_sha": self.last_commit_sha,
            "last_indexed_at": self.last_indexed_at,
            "file_index": {k: asdict(v) for k, v in self.file_index.items()},
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.warning("Could not save index state to %s: %s", path, exc)

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
