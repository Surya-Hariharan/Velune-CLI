"""Build and restore a single portable archive of all Velune state.

The archive is a ``.tar.gz`` whose top level holds a ``manifest.json`` plus one
folder per subsystem:

    manifest.json
    sessions/<id>.json ...
    config/workspace_velune.toml
    config/workspace_config.toml
    config/home_velune.toml
    providers/providers.json       (secrets excluded — keys masked as "***")
    providers/providers.json.enc   (secrets included — AES-GCM, passphrase-derived key)
    memory/velune_cognitive_core.db
    memory/lancedb_semantic_store/...
    trust/trusted_dirs.json

Restore always targets the *current* machine's resolved locations (computed from
``velune.core.paths`` and the active workspace), not the paths recorded on the
machine that produced the backup — so a snapshot can be recovered onto a fresh
install. A backup is staged into a temp directory and then tarred, rather than
streamed, so the SQLite cognitive core can be copied with the consistent
``sqlite3.Connection.backup()`` API before it is added to the archive.

Provider API keys are never written to an archive in the clear. By default
they are excluded from ``providers.json`` entirely (masked as ``"***"``); the
``with_secrets``/passphrase path in :func:`create_backup` instead encrypts
them with AES-GCM using a key derived from a caller-supplied passphrase (see
``velune.providers.crypto.encrypt_with_passphrase``) and writes the result to
``providers.json.enc``. That passphrase is never stored — the caller must
supply it again to :func:`restore_backup` to decrypt. This is deliberately a
different key path than the OS-keyring-backed key that protects the live
``credentials.json`` store, so an exported archive stays portable to a fresh
install (which may have no keyring entry, or a different one).
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

# Provider export filenames (relative to the "providers/" folder in the
# archive). The encrypted form is used whenever secrets are included; the
# plain form only ever holds masked ("***") key fields.
_PROVIDERS_ENC_NAME = "providers.json.enc"
_PROVIDERS_PLAIN_NAME = "providers.json"


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
    with_secrets: bool = False,
    secrets_passphrase: str | None = None,
    workspace: Path | None = None,
) -> BackupResult:
    """Snapshot the selected subsystems into a ``.tar.gz`` at *dest*.

    *include* defaults to all of :data:`SUBSYSTEMS`. Provider API keys are
    masked (``"***"``) in the embedded provider export unless *with_secrets*
    is True. When *with_secrets* is True, *secrets_passphrase* is required and
    the provider export is AES-GCM encrypted with a key derived from it —
    keys are never written to disk in the clear. The passphrase is not stored
    anywhere; it must be supplied again on restore.
    """
    if with_secrets and not secrets_passphrase:
        raise ValueError(
            "secrets_passphrase is required when with_secrets=True — "
            "provider API keys are never written to an archive unencrypted."
        )

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
            result.subsystems["providers"] = _backup_providers(
                stage, with_secrets, secrets_passphrase
            )
        if "memory" in selected:
            result.subsystems["memory"] = _backup_memory(stage, ws)
        if "trust" in selected:
            result.subsystems["trust"] = _backup_trust(stage)

        manifest = {
            "version": MANIFEST_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "velune_version": _velune_version(),
            "workspace": str(ws),
            # `with_secrets` here is the *boolean flag* (were secrets requested
            # for this backup), never the passphrase or a key value — CodeQL's
            # py/clear-text-storage-sensitive-data query name-matches "secret"
            # and flags this bool as if it carried the credential itself; see
            # the module docstring for the actual (already-encrypted) path.
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


def _backup_providers(stage: Path, with_secrets: bool, passphrase: str | None) -> dict:
    from velune.providers.crypto import encrypt_with_passphrase
    from velune.providers.keystore import export_provider_metadata, export_provider_secrets

    out = stage / "providers"
    out.mkdir(parents=True, exist_ok=True)

    # `metadata` is built by a function that has no expression anywhere in its
    # body reading a real key value (see its docstring) — every "key" field is
    # the literal "***". It is the *only* source of `provider_ids` below, in
    # both branches, so the manifest summary this function returns can never
    # carry secret material regardless of `with_secrets`. The real key values
    # (from `export_provider_secrets()`) are fetched only inside the `if`
    # branch and flow solely into `encrypt_with_passphrase()` — they are never
    # in scope where a plaintext write happens.
    metadata = export_provider_metadata()

    if with_secrets:
        assert passphrase is not None  # enforced by create_backup
        secrets_payload = {
            "version": "2.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "with_secrets": True,
            "providers": export_provider_secrets(),
        }
        blob = encrypt_with_passphrase(json.dumps(secrets_payload), passphrase)
        (out / _PROVIDERS_ENC_NAME).write_bytes(blob)
    else:
        plain_payload = {
            "version": "2.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "with_secrets": False,
            "providers": metadata,
        }
        (out / _PROVIDERS_PLAIN_NAME).write_text(
            json.dumps(plain_payload, indent=2), encoding="utf-8"
        )

    provider_ids = sorted(metadata)
    return {
        "present": bool(provider_ids),
        "providers": provider_ids,
        "encrypted": with_secrets,
    }


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
    secrets_passphrase: str | None = None,
) -> RestoreResult:
    """Restore selected subsystems from *archive* onto this machine.

    Existing session/config/trust files are kept unless *overwrite* is True.
    When *dry_run* is True nothing is written; the planned actions are returned.
    If the archive holds an encrypted provider-secrets payload, it is only
    decrypted when *secrets_passphrase* matches the one used at backup time;
    otherwise the providers subsystem is skipped (other subsystems still
    restore normally).
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
            if name == "providers":
                restored, skipped = _restore_providers(
                    extract, ws, overwrite, dry_run, secrets_passphrase
                )
            else:
                handler = {
                    "sessions": _restore_sessions,
                    "config": _restore_config,
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
    extract: Path,
    workspace: Path,
    overwrite: bool,
    dry_run: bool,
    secrets_passphrase: str | None = None,
) -> tuple[list[str], list[str]]:
    enc_src = extract / "providers" / _PROVIDERS_ENC_NAME
    plain_src = extract / "providers" / _PROVIDERS_PLAIN_NAME

    if enc_src.is_file():
        if not secrets_passphrase:
            return [], ["providers (passphrase required to decrypt secrets)"]
        from velune.providers.crypto import DecryptionError, decrypt_with_passphrase

        try:
            payload = json.loads(decrypt_with_passphrase(enc_src.read_bytes(), secrets_passphrase))
        except DecryptionError:
            return [], ["providers (wrong passphrase)"]
    elif plain_src.is_file():
        # Legacy/no-secrets format: masked keys, or (pre-fix archives only)
        # plaintext keys from a backup made before secrets were encrypted.
        payload = json.loads(plain_src.read_text(encoding="utf-8"))
    else:
        return [], []

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


def archive_has_encrypted_secrets(archive: Path) -> bool:
    """Return True if *archive* holds an encrypted provider-secrets payload.

    Lets callers (CLI/REPL) decide whether to prompt for a passphrase before
    calling :func:`restore_backup`, without extracting the whole archive.
    """
    with tarfile.open(archive, "r:gz") as tar:
        suffix = f"providers/{_PROVIDERS_ENC_NAME}"
        return any(name.endswith(suffix) for name in tar.getnames())


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract *tar* into *dest*, refusing any member that escapes *dest*."""
    dest = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        # A plain `str(...).startswith(str(dest))` check (the naive zip-slip
        # guard) is a path-*prefix* test, not a containment test: a sibling
        # directory like `dest`'s-parent/`<dest.name>-evil/...` shares the
        # same string prefix as `dest` without being inside it, so a crafted
        # member name could still escape. Match the containment check
        # `PathGuard.validate()` uses instead (velune/execution/path_guard.py).
        if member_path != dest and dest not in member_path.parents:
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
