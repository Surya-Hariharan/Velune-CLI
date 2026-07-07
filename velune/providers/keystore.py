"""Authoritative API key storage layer.

Provides a unified, cross-platform, encrypted credential manager backed by a
single JSON configuration file. Supports atomic file writes to prevent corruption
and merges updates rather than overwriting unrelated providers.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from platformdirs import user_config_dir

from velune.providers.crypto import decrypt_credentials, encrypt_credentials

logger = logging.getLogger("velune.providers.keystore")

PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "huggingface": "HF_TOKEN",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}


class CredentialManager:
    """Manages the encrypted credentials.json file."""

    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self) -> None:
        self._config_dir = Path(user_config_dir("Velune"))
        self._credentials_file = self._config_dir / "credentials.json"
        self._cache: dict[str, dict[str, Any]] | None = None
        self._cache_lock = Lock()

    def _load_disk(self, retry: bool = True) -> dict[str, dict[str, Any]]:
        """Load and decrypt the configuration from disk."""
        if not self._credentials_file.exists():
            return {}

        try:
            encrypted_data = self._credentials_file.read_bytes()
            if not encrypted_data:
                return {}
            json_str = decrypt_credentials(encrypted_data)
            data = json.loads(json_str)
            return data.get("providers", {})
        except Exception as e:
            # Non-fatal: an unreadable credentials file degrades gracefully to
            # environment variables / "no providers configured", and the CLI
            # guides the user to `velune setup` downstream. Try a backup restore
            # first; only if that also fails do we log — at debug, so a transient
            # key change doesn't spam an ERROR line on every command. Use
            # `velune doctor` or `--verbose` to surface it.
            if retry and self._attempt_backup_restore():
                return self._load_disk(retry=False)
            logger.debug("Could not load stored credentials: %s", e)
            return {}

    def _attempt_backup_restore(self) -> bool:
        """Attempt to recover from a backup file if primary is corrupted."""
        backup = self._credentials_file.with_name("credentials.json.bak")
        if backup.exists():
            try:
                shutil.copy2(backup, self._credentials_file)
                logger.info("Restored credentials from backup.")
                return True
            except Exception as e:
                logger.error("Failed to restore backup: %s", e)
        return False

    def _save_disk(self, providers: dict[str, dict[str, Any]]) -> None:
        """Encrypt and atomically save the configuration to disk."""
        self._config_dir.mkdir(parents=True, exist_ok=True)

        # Read the most recent disk state to merge updates instead of overwriting
        disk_data = self._load_disk()
        disk_data.update(providers)

        data_wrapper = {"providers": disk_data}
        json_str = json.dumps(data_wrapper, indent=2)
        encrypted_data = encrypt_credentials(json_str)

        # Atomic write sequence: Temp file -> fsync -> rename
        temp_file = self._credentials_file.with_suffix(".tmp")
        try:
            temp_file.write_bytes(encrypted_data)

            # Flush and sync to disk
            with open(temp_file, "r+b") as f:
                f.flush()
                os.fsync(f.fileno())

            # Create backup of known good state
            if self._credentials_file.exists():
                backup_file = self._credentials_file.with_name("credentials.json.bak")
                shutil.copy2(self._credentials_file, backup_file)

            # Atomic replace
            temp_file.replace(self._credentials_file)

            # Secure file permissions (Linux/macOS)
            if os.name != "nt":
                self._credentials_file.chmod(0o600)
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        """Get the full record for a provider."""
        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()
            return self._cache.get(provider_id)

    def save_provider(self, provider_id: str, key: str, status: str = "valid") -> None:
        """Save a provider's key and metadata."""
        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()

            self._cache[provider_id] = {
                "key": key,
                "status": status,
                "last_verified": datetime.now(timezone.utc).isoformat(),
            }
            self._save_disk(self._cache)

    def delete_provider(self, provider_id: str) -> None:
        """Delete a provider from the configuration."""
        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()

            if provider_id in self._cache:
                del self._cache[provider_id]
                self._save_disk(self._cache)

    def get_all_providers(self) -> dict[str, dict[str, Any]]:
        """Get a copy of all loaded providers."""
        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()
            return dict(self._cache)


_manager = CredentialManager()


def save_key(provider_id: str, api_key: str) -> None:
    """Persist *api_key* for *provider_id*."""
    _manager.save_provider(provider_id, api_key)


def get_key(provider_id: str) -> str | None:
    """Return the API key for *provider_id* (Environment var first, then config)."""
    env_var = PROVIDER_ENV_VARS.get(provider_id)
    if env_var:
        val = os.getenv(env_var)
        if val:
            return val

    record = _manager.get_provider(provider_id)
    if record and "key" in record:
        return record["key"]
    return None


def delete_key(provider_id: str) -> None:
    """Remove the stored key for *provider_id*."""
    _manager.delete_provider(provider_id)


def has_key(provider_id: str) -> bool:
    """Return True if a key is available for *provider_id*."""
    return get_key(provider_id) is not None


def is_ollama_live(timeout: float = 0.25) -> bool:
    """Return True if a local Ollama server is reachable."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=timeout):
            return True
    except OSError:
        return False


def list_configured_providers(include_ollama: bool = True) -> list[str]:
    """Return provider IDs that have a usable key.

    Environment variables are checked first, followed by the local JSON configuration.
    """
    configured: set[str] = set()

    for pid, env_var in PROVIDER_ENV_VARS.items():
        if os.getenv(env_var):
            configured.add(pid)

    disk_providers = _manager.get_all_providers()
    for pid in disk_providers:
        configured.add(pid)

    ordered = [pid for pid in PROVIDER_ENV_VARS if pid in configured]

    if include_ollama and is_ollama_live():
        ordered.insert(0, "ollama")
    return ordered


def get_provider_status(provider_id: str) -> dict[str, Any]:
    """Diagnostic helper for returning a provider's state and configuration info."""
    record = _manager.get_provider(provider_id)
    env_var = PROVIDER_ENV_VARS.get(provider_id)
    is_env = bool(env_var and os.getenv(env_var))

    if is_env:
        return {
            "stored": True,
            "source": "environment",
            "status": "valid",
            "last_verified": "dynamic",
            "location": f"${env_var}",
        }

    if record:
        return {
            "stored": True,
            "source": "config",
            "status": record.get("status", "unknown"),
            "last_verified": record.get("last_verified", "unknown"),
            "location": str(_manager._credentials_file),
        }

    return {
        "stored": False,
        "source": "none",
        "status": "missing",
        "last_verified": "n/a",
        "location": "n/a",
    }


def credentials_file_path() -> Path:
    """Return the path to the encrypted credentials file."""
    return _manager._credentials_file


def export_providers_json(include_keys: bool = True) -> dict[str, Any]:
    """Produce a serialisable snapshot of all configured providers.

    Used by ``velune provider backup`` to write a portable JSON export.
    """
    snap: dict[str, Any] = {}

    for pid, rec in _manager.get_all_providers().items():
        snap[pid] = {
            "key": rec.get("key", "") if include_keys else "***",
            "status": rec.get("status", "unknown"),
            "last_verified": rec.get("last_verified", ""),
            "source": "file",
        }

    for pid, env_var in PROVIDER_ENV_VARS.items():
        if pid not in snap:
            val = os.getenv(env_var)
            if val:
                snap[pid] = {
                    "key": val if include_keys else "***",
                    "status": "env",
                    "last_verified": "dynamic",
                    "source": "environment",
                    "env_var": env_var,
                }

    return snap


def import_providers_json(
    records: dict[str, Any], overwrite: bool = False
) -> tuple[list[str], list[str]]:
    """Merge provider records from a backup JSON into the credential store.

    Returns ``(imported, skipped)`` lists of provider IDs.
    """
    imported: list[str] = []
    skipped: list[str] = []

    for pid, entry in records.items():
        key = entry.get("key", "").strip()
        if not key or key == "***":
            skipped.append(pid)
            continue
        if not overwrite and has_key(pid):
            skipped.append(pid)
            continue
        _manager.save_provider(pid, key, status=entry.get("status", "imported"))
        imported.append(pid)

    return imported, skipped


def repair_keystore() -> dict[str, Any]:
    """Attempt to repair a corrupted credentials store.

    Steps:
    1. If the in-memory cache is empty, force a reload from disk.
    2. If disk is also empty, try restoring from the auto-backup (.bak).
    3. Remove any records that have an empty key.

    Returns a report dict with keys ``restored_backup``, ``removed``, ``kept``.
    """
    report: dict[str, Any] = {"restored_backup": False, "removed": [], "kept": []}

    # Force a fresh disk read.
    with _manager._cache_lock:
        _manager._cache = None

    try:
        current = _manager.get_all_providers()
    except Exception:
        current = {}

    if not current:
        restored = _manager._attempt_backup_restore()
        if restored:
            with _manager._cache_lock:
                _manager._cache = None
            report["restored_backup"] = True
            try:
                current = _manager.get_all_providers()
            except Exception:
                current = {}

    # Purge records with empty/missing keys.
    to_remove = [pid for pid, rec in current.items() if not rec.get("key")]
    for pid in to_remove:
        _manager.delete_provider(pid)
        report["removed"].append(pid)

    report["kept"] = [pid for pid in current if pid not in to_remove]
    return report
