"""Platform-compliant storage paths for Velune.

Heavy, frequently-written state (the SQLite cognitive core and the Qdrant
vector store) must NOT live inside the workspace tree. On Windows the
workspace is often under a cloud-synced folder (OneDrive, Dropbox, Google
Drive). Every file touch then triggers a sync-engine reparse, multiplying
I/O latency by 3-10x and serializing startup behind metadata syncs. This was
the dominant contributor to Velune's ~78s cold start.

This module centralizes storage resolution so all subsystems agree on one
location, and relocates heavy state to the platform-native, *non-synced*
application-data directory:

    Windows  -> %LOCALAPPDATA%\\Velune
    macOS    -> ~/Library/Application Support/Velune
    Linux    -> $XDG_DATA_HOME/velune  (default ~/.local/share/velune)

State is still isolated *per workspace* (so two projects never share a
cognitive core) by hashing the workspace's resolved absolute path into a
stable, human-readable slug under ``<data_root>/workspaces/``.

Lightweight, human-facing files (``velune.toml``, ``.veluneignore``,
snapshots, sessions) intentionally stay in the workspace ``.velune/`` dir —
they're tiny, rarely written, and users expect them project-local.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import shutil
from pathlib import Path

logger = logging.getLogger("velune.core.paths")

_APP_NAME = "Velune"

# Heavy state filenames relocated off the workspace tree.
COGNITIVE_DB_NAME = "velune_cognitive_core.db"
QDRANT_STORE_NAME = "qdrant_local_store"

# Marker written into a relocated workspace dir once legacy data has been
# migrated, so migration runs at most once per workspace.
_MIGRATION_MARKER = ".migrated_from_workspace"


def app_data_root() -> Path:
    """Return the platform-native, non-synced application data root.

    Honors ``VELUNE_DATA_HOME`` as an explicit override (useful for tests,
    CI, and power users who want all state in one place).
    """
    override = os.environ.get("VELUNE_DATA_HOME")
    if override:
        return Path(override).expanduser()

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / _APP_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / _APP_NAME
    # Linux / other POSIX
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "velune"


def _workspace_slug(workspace: Path) -> str:
    """Build a stable, collision-resistant, readable slug for *workspace*.

    Combines a sanitized directory name with a short hash of the resolved
    absolute path so distinct projects with the same folder name never
    collide.
    """
    try:
        resolved = workspace.resolve()
    except Exception:
        resolved = workspace.absolute()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:10]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", resolved.name) or "workspace"
    return f"{name}-{digest}"


def workspace_storage_dir(workspace: Path) -> Path:
    """Return (creating) the non-synced storage dir for *workspace*."""
    target = app_data_root() / "workspaces" / _workspace_slug(workspace)
    target.mkdir(parents=True, exist_ok=True)
    return target


def cognitive_db_path(workspace: Path) -> Path:
    """Absolute path to the workspace's SQLite cognitive core (non-synced)."""
    return workspace_storage_dir(workspace) / COGNITIVE_DB_NAME


def qdrant_store_path(workspace: Path) -> Path:
    """Absolute path to the workspace's Qdrant local store (non-synced)."""
    return workspace_storage_dir(workspace) / QDRANT_STORE_NAME


LANCEDB_STORE_NAME = "lancedb_semantic_store"


def lancedb_store_path(workspace: Path) -> Path:
    """Absolute path to the workspace's LanceDB semantic store (non-synced)."""
    return workspace_storage_dir(workspace) / LANCEDB_STORE_NAME


def legacy_workspace_dir(workspace: Path) -> Path:
    """The old in-workspace ``.velune`` directory (may be cloud-synced)."""
    return workspace / ".velune"


def migrate_legacy_storage(workspace: Path) -> bool:
    """One-time, best-effort relocation of heavy state out of the workspace.

    Copies (does not move) any pre-existing ``.velune/velune_cognitive_core.db``
    and ``.velune/qdrant_local_store`` into the non-synced storage dir, leaving
    the originals untouched as a safety backup. Writes a marker so this runs at
    most once per workspace. Returns True if anything was migrated.

    Copy-then-mark (rather than move) is deliberate: an interrupted move could
    corrupt a live cognitive core, whereas a partial copy is simply retried.
    """
    target = workspace_storage_dir(workspace)
    marker = target / _MIGRATION_MARKER
    if marker.exists():
        return False

    legacy = legacy_workspace_dir(workspace)
    migrated = False

    legacy_db = legacy / COGNITIVE_DB_NAME
    new_db = target / COGNITIVE_DB_NAME
    if legacy_db.exists() and not new_db.exists():
        try:
            # Bring along WAL/SHM sidecars if present for a consistent copy.
            for suffix in ("", "-wal", "-shm"):
                src = legacy / f"{COGNITIVE_DB_NAME}{suffix}"
                if src.exists():
                    shutil.copy2(src, target / src.name)
            migrated = True
            logger.info("Migrated cognitive core from %s -> %s", legacy_db, new_db)
        except Exception as exc:
            logger.warning("Cognitive core migration skipped: %s", exc)

    legacy_qdrant = legacy / QDRANT_STORE_NAME
    new_qdrant = target / QDRANT_STORE_NAME
    if legacy_qdrant.is_dir() and not new_qdrant.exists():
        try:
            shutil.copytree(legacy_qdrant, new_qdrant)
            migrated = True
            logger.info("Migrated vector store from %s -> %s", legacy_qdrant, new_qdrant)
        except Exception as exc:
            logger.warning("Vector store migration skipped: %s", exc)

    try:
        marker.write_text("ok\n", encoding="utf-8")
    except Exception:
        pass
    return migrated
