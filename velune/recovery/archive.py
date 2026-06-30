"""Build and restore a single portable archive of all Velune state.

The archive is a ``.tar.gz`` whose top level holds a ``manifest.json`` plus one
folder per subsystem:

    manifest.json
    sessions/<id>.json ...
    config/workspace_velune.toml
    config/workspace_config.toml
    config/home_velune.toml
    providers/providers.json
    memory/velune_cognitive_core.db
    memory/lancedb_semantic_store/...
    trust/trusted_dirs.json

Restore always targets the *current* machine's resolved locations (computed from
``velune.core.paths`` and the active workspace), not the paths recorded on the
machine that produced the backup — so a snapshot can be recovered onto a fresh
install. A backup is staged into a temp directory and then tarred, rather than
streamed, so the SQLite cognitive core can be copied with the consistent
``sqlite3.Connection.backup()`` API before it is added to the archive.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from velune.core.paths import (
    COGNITIVE_DB_NAME,
    LANCEDB_STORE_NAME,
    cognitive_db_path,
    lancedb_store_path,
)
from velune.core.trust import trust_file_path

_log = logging.getLogger("velune.recovery.archive")

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = "1.0"

# Logical subsystems a backup can include. Order is the order they are written.
SUBSYSTEMS: tuple[str, ...] = ("sessions", "config", "providers", "memory", "trust")

# Fixed archive names for the three config slots, so restore knows each target.
_CFG_WORKSPACE_TOML = "config/workspace_velune.toml"
_CFG_WORKSPACE_DOT = "config/workspace_config.toml"
_CFG_HOME_TOML = "config/home_velune.toml"


@dataclass(slots=True)
class BackupResult:
    """Outcome of :func:`create_backup`."""

    path: Path
    subsystems: dict[str, dict] = field(default_factory=dict)
    with_secrets: bool = False
    size_bytes: int = 0


@dataclass(slots=True)
class RestoreResult:
    """Outcome of :func:`restore_backup`."""

    restored: dict[str, list[str]] = field(default_factory=dict)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    dry_run: bool = False
    manifest: dict = field(default_factory=dict)


# ── Location helpers (current-machine targets) ───────────────────────────────


def _sessions_dir() -> Path:
    from velune.cli.sessions import DEFAULT_SESSIONS_DIR

    return DEFAULT_SESSIONS_DIR


def _config_sources(workspace: Path) -> list[tuple[str, Path]]:
    """Return ``(archive_name, source_path)`` pairs for config files that exist."""
    candidates = [
        (_CFG_WORKSPACE_TOML, workspace / "velune.toml"),
        (_CFG_WORKSPACE_DOT, workspace / ".velune" / "config.toml"),
        (_CFG_HOME_TOML, Path.home() / ".velune" / "velune.toml"),
    ]
    return [(name, path) for name, path in candidates if path.is_file()]


def _config_target(arcname: str, workspace: Path) -> Path:
    return {
        _CFG_WORKSPACE_TOML: workspace / "velune.toml",
        _CFG_WORKSPACE_DOT: workspace / ".velune" / "config.toml",
        _CFG_HOME_TOML: Path.home() / ".velune" / "velune.toml",
    }[arcname]


# ── Backup ───────────────────────────────────────────────────────────────────


def create_backup(
    dest: Path,
    include: set[str] | None = None,
    *,
    with_secrets: bool = True,
    workspace: Path | None = None,
) -> BackupResult:
    """Snapshot the selected subsystems into a ``.tar.gz`` at *dest*.

    *include* defaults to all of :data:`SUBSYSTEMS`. When *with_secrets* is
    False, provider API keys are masked in the embedded provider export.
    """
    selected = _normalize_include(include)
    ws = (workspace or Path.cwd()).resolve()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = BackupResult(path=dest, with_secrets=with_secrets)

    with tempfile.TemporaryDirectory(prefix="velune-backup-") as tmp:
        stage = Path(tmp)
        if "sessions" in selected:
            result.subsystems["sessions"] = _backup_sessions(stage)
        if "config" in selected:
            result.subsystems["config"] = _backup_config(stage, ws)
        if "providers" in selected:
            result.subsystems["providers"] = _backup_providers(stage, with_secrets)
        if "memory" in selected:
            result.subsystems["memory"] = _backup_memory(stage, ws)
        if "trust" in selected:
            result.subsystems["trust"] = _backup_trust(stage)

        manifest = {
            "version": MANIFEST_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "velune_version": _velune_version(),
            "workspace": str(ws),
            "with_secrets": with_secrets,
            "subsystems": result.subsystems,
        }
        (stage / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        with tarfile.open(dest, "w:gz") as tar:
            tar.add(stage, arcname=".")

    result.size_bytes = dest.stat().st_size
    return result


def _backup_sessions(stage: Path) -> dict:
    src_dir = _sessions_dir()
    files: list[str] = []
    if src_dir.is_dir():
        out = stage / "sessions"
        out.mkdir(parents=True, exist_ok=True)
        for f in src_dir.glob("*.json"):
            shutil.copy2(f, out / f.name)
            files.append(f"sessions/{f.name}")
    return {"files": sorted(files)}


def _backup_config(stage: Path, workspace: Path) -> dict:
    files: list[str] = []
    for arcname, src in _config_sources(workspace):
        dst = stage / arcname
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        files.append(arcname)
    return {"files": sorted(files)}


def _backup_providers(stage: Path, with_secrets: bool) -> dict:
    from velune.providers.keystore import export_providers_json

    snapshot = export_providers_json(include_keys=with_secrets)
    out = stage / "providers"
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "with_secrets": with_secrets,
        "providers": snapshot,
    }
    (out / "providers.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"present": bool(snapshot), "providers": sorted(snapshot.keys())}


def _backup_memory(stage: Path, workspace: Path) -> dict:
    out = stage / "memory"
    summary: dict = {"db": False, "lancedb": False}

    db_path = cognitive_db_path(workspace)
    if db_path.is_file():
        out.mkdir(parents=True, exist_ok=True)
        _copy_sqlite_consistent(db_path, out / COGNITIVE_DB_NAME)
        summary["db"] = True

    lance_path = lancedb_store_path(workspace)
    if lance_path.is_dir() and any(lance_path.iterdir()):
        out.mkdir(parents=True, exist_ok=True)
        shutil.copytree(lance_path, out / LANCEDB_STORE_NAME)
        summary["lancedb"] = True

    return summary


def _backup_trust(stage: Path) -> dict:
    src = trust_file_path()
    if not src.is_file():
        return {"files": []}
    out = stage / "trust"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out / "trusted_dirs.json")
    return {"files": ["trust/trusted_dirs.json"]}


def _copy_sqlite_consistent(src: Path, dst: Path) -> None:
    """Copy a (possibly open, WAL-mode) SQLite DB to *dst* consistently."""
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


# ── Restore ──────────────────────────────────────────────────────────────────


def restore_backup(
    archive: Path,
    include: set[str] | None = None,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    workspace: Path | None = None,
) -> RestoreResult:
    """Restore selected subsystems from *archive* onto this machine.

    Existing session/config/trust files are kept unless *overwrite* is True.
    When *dry_run* is True nothing is written; the planned actions are returned.
    """
    archive = Path(archive)
    if not archive.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive}")

    selected = _normalize_include(include)
    ws = (workspace or Path.cwd()).resolve()
    result = RestoreResult(dry_run=dry_run)

    with tempfile.TemporaryDirectory(prefix="velune-restore-") as tmp:
        extract = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, extract)

        manifest_path = extract / MANIFEST_NAME
        if not manifest_path.is_file():
            raise ValueError("Archive is missing manifest.json — not a Velune backup.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result.manifest = manifest
        present = set(manifest.get("subsystems", {}))

        for name in SUBSYSTEMS:
            if name not in selected or name not in present:
                continue
            handler = {
                "sessions": _restore_sessions,
                "config": _restore_config,
                "providers": _restore_providers,
                "memory": _restore_memory,
                "trust": _restore_trust,
            }[name]
            restored, skipped = handler(extract, ws, overwrite, dry_run)
            result.restored[name] = restored
            if skipped:
                result.skipped[name] = skipped

    return result


def _restore_sessions(
    extract: Path, workspace: Path, overwrite: bool, dry_run: bool
) -> tuple[list[str], list[str]]:
    return _restore_dir(extract / "sessions", _sessions_dir(), overwrite, dry_run, label="session")


def _restore_config(
    extract: Path, workspace: Path, overwrite: bool, dry_run: bool
) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    skipped: list[str] = []
    for arcname in (_CFG_WORKSPACE_TOML, _CFG_WORKSPACE_DOT, _CFG_HOME_TOML):
        src = extract / arcname
        if not src.is_file():
            continue
        target = _config_target(arcname, workspace)
        _place_file(src, target, overwrite, dry_run, restored, skipped, label=target.name)
    return restored, skipped


def _restore_providers(
    extract: Path, workspace: Path, overwrite: bool, dry_run: bool
) -> tuple[list[str], list[str]]:
    src = extract / "providers" / "providers.json"
    if not src.is_file():
        return [], []
    payload = json.loads(src.read_text(encoding="utf-8"))
    records = payload.get("providers", {})
    if dry_run:
        return [f"import {pid}" for pid in sorted(records)], []
    from velune.providers.keystore import import_providers_json

    imported, skipped = import_providers_json(records, overwrite=overwrite)
    return sorted(imported), sorted(skipped)


def _restore_memory(
    extract: Path, workspace: Path, overwrite: bool, dry_run: bool
) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    skipped: list[str] = []

    db_src = extract / "memory" / COGNITIVE_DB_NAME
    if db_src.is_file():
        _place_file(
            db_src,
            cognitive_db_path(workspace),
            overwrite,
            dry_run,
            restored,
            skipped,
            label="cognitive DB",
        )

    lance_src = extract / "memory" / LANCEDB_STORE_NAME
    if lance_src.is_dir():
        target = lancedb_store_path(workspace)
        if target.exists() and not overwrite:
            skipped.append("LanceDB store (exists)")
        elif dry_run:
            restored.append(f"restore LanceDB -> {target}")
        else:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(lance_src, target)
            restored.append("LanceDB store")

    return restored, skipped


def _restore_trust(
    extract: Path, workspace: Path, overwrite: bool, dry_run: bool
) -> tuple[list[str], list[str]]:
    src = extract / "trust" / "trusted_dirs.json"
    if not src.is_file():
        return [], []
    restored: list[str] = []
    skipped: list[str] = []
    _place_file(src, trust_file_path(), overwrite, dry_run, restored, skipped, label="trust list")
    return restored, skipped


# ── Restore helpers ──────────────────────────────────────────────────────────


def _restore_dir(
    src_dir: Path, target_dir: Path, overwrite: bool, dry_run: bool, *, label: str
) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    skipped: list[str] = []
    if not src_dir.is_dir():
        return restored, skipped
    for f in sorted(src_dir.glob("*.json")):
        target = target_dir / f.name
        _place_file(f, target, overwrite, dry_run, restored, skipped, label=f"{label} {f.stem}")
    return restored, skipped


def _place_file(
    src: Path,
    target: Path,
    overwrite: bool,
    dry_run: bool,
    restored: list[str],
    skipped: list[str],
    *,
    label: str,
) -> None:
    if target.exists() and not overwrite:
        skipped.append(f"{label} (exists)")
        return
    if dry_run:
        restored.append(f"restore {label} -> {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    restored.append(label)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract *tar* into *dest*, refusing any member that escapes *dest*."""
    dest = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        if not str(member_path).startswith(str(dest)):
            raise ValueError(f"Unsafe path in archive: {member.name}")
    try:
        # Python 3.12+ supports the "data" extraction filter; members are also
        # validated above for older runtimes that lack the kwarg.
        tar.extractall(dest, filter="data")  # nosec B202 - members validated above
    except TypeError:
        tar.extractall(dest)  # nosec B202 - members validated above


# ── Misc ─────────────────────────────────────────────────────────────────────


def _normalize_include(include: set[str] | None) -> set[str]:
    if not include:
        return set(SUBSYSTEMS)
    unknown = include - set(SUBSYSTEMS)
    if unknown:
        raise ValueError(
            f"Unknown subsystem(s): {', '.join(sorted(unknown))}. Valid: {', '.join(SUBSYSTEMS)}"
        )
    return set(include)


def _velune_version() -> str:
    try:
        from velune import __version__

        return str(__version__)
    except Exception:
        return "unknown"
