"""Build a truthful, inspectable report of Velune's repository context state.

``velune context`` exists to answer one suspicion directly: *is the project
context real, or is it placeholder logic?* This module assembles the answer from
data Velune actually persists — the incremental index state at
``.velune/index_state.json``, the per-file symbol cache, the cognitive SQLite
core, and the on-disk vector stores — plus a live ``git`` comparison to prove
the index is fresh (or honestly report that it is stale).

The builder is intentionally **pure and runtime-free**: it reads files and runs
read-only git/SQLite queries, so the command stays fast and works even when the
full runtime has never been started. Every field is derived from real state; an
absent index yields ``index_exists=False`` and zeroed counts, never invented
numbers.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from velune.core.paths import (
    cognitive_db_path,
    lancedb_store_path,
    qdrant_store_path,
)

# NOTE: ``velune.repository`` and ``velune.cli`` form an import cycle (the
# repository package __init__ eventually imports the CLI). Importing repository
# symbols lazily inside :func:`build_context_report` keeps this module safe to
# import from a CLI command module.

_STATE_FILENAME = "index_state.json"
_CACHE_FILENAME = "index_cache.json"


@dataclass
class LanguageStat:
    """Per-language indexed-file and symbol counts."""

    language: str
    files: int
    symbols: int


@dataclass
class StorageStat:
    """Footprint of one persisted store on disk."""

    name: str
    path: str
    exists: bool
    size_bytes: int


@dataclass
class MemoryTableStat:
    """Row count for one table in the cognitive SQLite core."""

    table: str
    rows: int


@dataclass
class ContextReport:
    """A fully-derived snapshot of repository context health.

    Every attribute is read from real on-disk state. See module docstring.
    """

    workspace: str
    git_branch: str | None
    head_sha: str | None
    indexed_head_sha: str | None
    index_exists: bool
    indexed_file_count: int
    total_symbols: int
    languages: list[LanguageStat]
    last_indexed_at: float | None
    freshness: str  # "synced" | "stale" | "unknown" | "no-index"
    working_tree_dirty: int  # files changed vs HEAD (-1 if unknown)
    top_areas: list[tuple[str, int]]  # (top-level component, file count)
    storage: list[StorageStat]
    memory_tables: list[MemoryTableStat]
    ignored_dirs: list[str]
    health: list[tuple[str, str]] = field(default_factory=list)  # (state, message)

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for ``--json`` output."""
        return {
            "workspace": self.workspace,
            "git_branch": self.git_branch,
            "head_sha": self.head_sha,
            "indexed_head_sha": self.indexed_head_sha,
            "index_exists": self.index_exists,
            "indexed_file_count": self.indexed_file_count,
            "total_symbols": self.total_symbols,
            "languages": [
                {"language": ls.language, "files": ls.files, "symbols": ls.symbols}
                for ls in self.languages
            ],
            "last_indexed_at": self.last_indexed_at,
            "freshness": self.freshness,
            "working_tree_dirty": self.working_tree_dirty,
            "top_areas": [{"area": a, "files": n} for a, n in self.top_areas],
            "storage": [
                {
                    "name": s.name,
                    "path": s.path,
                    "exists": s.exists,
                    "size_bytes": s.size_bytes,
                }
                for s in self.storage
            ],
            "memory_tables": [{"table": t.table, "rows": t.rows} for t in self.memory_tables],
            "ignored_dirs": self.ignored_dirs,
            "health": [{"state": st, "message": msg} for st, msg in self.health],
        }


def _git(workspace: Path, args: list[str]) -> str | None:
    """Run a read-only git command in *workspace*, returning stdout or None."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=str(workspace),
            timeout=5,
            encoding="utf-8",
            errors="ignore",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _dir_size(path: Path) -> int:
    """Total size in bytes of *path* (file or directory tree). 0 if missing."""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _read_memory_tables(db_path: Path) -> list[MemoryTableStat]:
    """List tables and row counts from the cognitive SQLite core, read-only.

    Opens the database in immutable read-only URI mode so inspection never
    touches a live writer's data. Returns an empty list on any failure (cold
    start, locked, corrupt) — the command shows "no records yet", not an error.
    """
    if not db_path.exists():
        return []
    stats: list[MemoryTableStat] = []
    try:
        uri = f"file:{db_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = [row[0] for row in cur.fetchall()]
        for table in tables:
            try:
                # Table names come from sqlite_master (trusted catalog), not user
                # input; quote-wrap defends against unusual identifiers anyway.
                count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                continue
            stats.append(MemoryTableStat(table=table, rows=int(count)))
    except sqlite3.Error:
        return stats
    finally:
        conn.close()
    return stats


def build_context_report(workspace: Path) -> ContextReport:
    """Assemble a :class:`ContextReport` for *workspace* from real on-disk state."""
    # Lazy imports: see the cycle note near the top of this module.
    from velune.repository.incremental_indexer import _ALWAYS_SKIP
    from velune.repository.index_state import IndexState

    workspace = workspace.resolve()
    velune_dir = workspace / ".velune"
    state_path = velune_dir / _STATE_FILENAME

    # --- Index state (the heart of "is context real") ---
    state = IndexState.load(state_path)
    index_exists = state is not None
    file_index = state.file_index if state else {}
    indexed_file_count = len(file_index)

    lang_files: Counter[str] = Counter()
    lang_symbols: Counter[str] = Counter()
    area_files: Counter[str] = Counter()
    total_symbols = 0
    for rel_path, entry in file_index.items():
        lang_files[entry.language] += 1
        lang_symbols[entry.language] += entry.symbol_count
        total_symbols += entry.symbol_count
        # Top-level path segment as a coarse "knowledge area".
        head = rel_path.replace("\\", "/").split("/", 1)[0]
        area_files[head] += 1

    languages = [
        LanguageStat(language=lang, files=n, symbols=lang_symbols[lang])
        for lang, n in lang_files.most_common()
    ]
    top_areas = area_files.most_common(6)

    # --- Git freshness comparison ---
    branch = _git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
    head_sha = _git(workspace, ["rev-parse", "HEAD"])
    indexed_head_sha = state.last_commit_sha if state else None

    dirty_out = _git(workspace, ["diff", "HEAD", "--name-only"])
    working_tree_dirty = (
        -1 if dirty_out is None else len([ln for ln in dirty_out.splitlines() if ln])
    )

    if not index_exists:
        freshness = "no-index"
    elif head_sha is None:
        freshness = "unknown"  # not a git repo / git unavailable — can't compare
    elif indexed_head_sha == head_sha and working_tree_dirty == 0:
        freshness = "synced"
    else:
        freshness = "stale"

    # --- Storage footprint (real file/dir sizes) ---
    storage = [
        StorageStat(
            "Index state",
            str(state_path),
            state_path.exists(),
            _dir_size(state_path),
        ),
        StorageStat(
            "Symbol cache",
            str(velune_dir / _CACHE_FILENAME),
            (velune_dir / _CACHE_FILENAME).exists(),
            _dir_size(velune_dir / _CACHE_FILENAME),
        ),
    ]
    cog_db = cognitive_db_path(workspace)
    storage.append(
        StorageStat("Cognitive core (SQLite)", str(cog_db), cog_db.exists(), _dir_size(cog_db))
    )
    for label, store in (
        ("Vector store (Qdrant)", qdrant_store_path(workspace)),
        ("Vector store (LanceDB)", lancedb_store_path(workspace)),
    ):
        storage.append(StorageStat(label, str(store), store.exists(), _dir_size(store)))

    memory_tables = _read_memory_tables(cog_db)

    # --- Health summary (derived, no fabrication) ---
    health: list[tuple[str, str]] = []
    if not index_exists:
        health.append(
            ("warn", "No index found — run a task or `velune` once to build the repository index.")
        )
    else:
        health.append(
            ("ok", f"Index present: {indexed_file_count} files, {total_symbols} symbols.")
        )
    if freshness == "synced":
        health.append(("ok", "Index is in sync with the current commit and clean working tree."))
    elif freshness == "stale":
        detail = []
        if indexed_head_sha != head_sha:
            detail.append("HEAD moved since last index")
        if working_tree_dirty > 0:
            detail.append(f"{working_tree_dirty} uncommitted file(s)")
        health.append(
            ("warn", "Index is stale (" + ", ".join(detail) + ") — will refresh on next run.")
        )
    elif freshness == "unknown":
        health.append(("warn", "Not a git repository — freshness cannot be verified by commit."))

    total_memory_rows = sum(t.rows for t in memory_tables)
    if cog_db.exists():
        health.append(
            (
                "ok",
                f"Cognitive core present: {total_memory_rows} record(s) across {len(memory_tables)} table(s).",
            )
        )
    else:
        health.append(
            ("warn", "Cognitive core not yet created — memory persists after the first session.")
        )

    return ContextReport(
        workspace=str(workspace),
        git_branch=branch,
        head_sha=head_sha,
        indexed_head_sha=indexed_head_sha,
        index_exists=index_exists,
        indexed_file_count=indexed_file_count,
        total_symbols=total_symbols,
        languages=languages,
        last_indexed_at=state.last_indexed_at if state else None,
        freshness=freshness,
        working_tree_dirty=working_tree_dirty,
        top_areas=top_areas,
        storage=storage,
        memory_tables=memory_tables,
        ignored_dirs=sorted(_ALWAYS_SKIP),
        health=health,
    )
