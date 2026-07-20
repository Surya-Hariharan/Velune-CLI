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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from platformdirs import user_config_dir

from velune._compat import StrEnum
from velune.providers.crypto import decrypt_credentials, encrypt_credentials

logger = logging.getLogger("velune.providers.keystore")

# How long a successful verification is trusted before the key is considered
# STALE and re-checked in the background. Stale never means unusable — see
# KeyState below.
VERIFY_TTL_SECONDS = 86_400  # 24h


class KeyState(StrEnum):
    """Lifecycle state of a provider credential, as surfaced to the user.

    This is the single predicate the UI should ask about. It is deliberately
    richer than the old ``has_key()`` boolean, which reported a revoked key as
    "configured".
    """

    VERIFIED = "verified"  # stored, accepted by the provider within the TTL
    UNVERIFIED = "unverified"  # stored, but never validated (save-anyway/--no-validate)
    STALE = "stale"  # was verified, but the TTL has elapsed — usable, re-check pending
    INVALID = "invalid"  # the provider actively rejected it
    MISSING = "missing"  # no key at all
    ENV = "env"  # supplied via environment variable; we don't own its lifecycle


# On-disk ``status`` values. Legacy records written before the lifecycle existed
# used "valid" (hardcoded on every save, including unvalidated ones) and
# "imported"; both are mapped on read rather than migrated, so an older
# credentials.json keeps working.
_STATUS_VERIFIED = "verified"
_STATUS_UNVERIFIED = "unverified"
_STATUS_INVALID = "invalid"

_LEGACY_STATUS_MAP: dict[str, str] = {
    "valid": _STATUS_VERIFIED,
    "imported": _STATUS_UNVERIFIED,
    "unknown": _STATUS_UNVERIFIED,
}

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
    "meta": "LLAMA_API_KEY",
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
        self._key_revisions: dict[str, int] = {}

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

    def _save_disk(
        self, providers: dict[str, dict[str, Any]], *, removed: set[str] | None = None
    ) -> None:
        """Encrypt and atomically save the configuration to disk.

        *providers* is merged onto the current on-disk state (rather than
        replacing it outright) so a concurrent save for a different provider
        isn't clobbered by a stale in-memory read. That merge is a plain
        ``dict.update()``, which can only add or overwrite keys — it has no
        way to express "this provider should no longer exist," since an
        entry that's merely absent from *providers* is indistinguishable from
        one nobody touched. *removed* closes that gap: those ids are popped
        from the merged result explicitly, which is what actually lets
        :meth:`delete_provider` persist a deletion instead of the disk read
        silently re-merging the just-deleted record back in.
        """
        self._config_dir.mkdir(parents=True, exist_ok=True)

        # Read the most recent disk state to merge updates instead of overwriting
        disk_data = self._load_disk()
        disk_data.update(providers)
        for provider_id in removed or ():
            disk_data.pop(provider_id, None)

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

    def save_provider(self, provider_id: str, key: str, status: str = _STATUS_UNVERIFIED) -> None:
        """Save a provider's key and its verification *status*.

        ``status`` defaults to UNVERIFIED, not VERIFIED: a caller that has not
        actually seen the provider accept this key must not be able to record it
        as verified by omission. Callers that *have* validated pass
        ``_STATUS_VERIFIED`` (or go through :func:`mark_verified`).

        ``last_verified`` is only stamped for a verified save — for an
        unverified or invalid one there is no verification to date, and writing
        "now" there would make a never-checked key look freshly checked.
        """
        # Accept the legacy spellings ("valid"/"imported") so an older caller or
        # a restored backup lands in the right state rather than silently
        # skipping the timestamp below and reading back as STALE forever.
        status = _LEGACY_STATUS_MAP.get(status, status)

        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()

            record: dict[str, Any] = {"key": key, "status": status}
            if status == _STATUS_VERIFIED:
                record["last_verified"] = datetime.now(timezone.utc).isoformat()
            self._cache[provider_id] = record
            self._save_disk(self._cache)
            self._bump_revision(provider_id)

    def update_status(self, provider_id: str, status: str, **extra: Any) -> None:
        """Rewrite a provider's verification status without touching its key."""
        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()

            record = self._cache.get(provider_id)
            if record is None:
                return

            record = dict(record)
            record["status"] = status
            record.pop("last_error", None)
            if status == _STATUS_VERIFIED:
                record["last_verified"] = datetime.now(timezone.utc).isoformat()
            record.update(extra)

            self._cache[provider_id] = record
            self._save_disk(self._cache)

    def delete_provider(self, provider_id: str) -> None:
        """Delete a provider from the configuration."""
        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()

            if provider_id in self._cache:
                del self._cache[provider_id]
                self._save_disk(self._cache, removed={provider_id})
                self._bump_revision(provider_id)

    def get_all_providers(self) -> dict[str, dict[str, Any]]:
        """Get a copy of all loaded providers."""
        with self._cache_lock:
            if self._cache is None:
                self._cache = self._load_disk()
            return dict(self._cache)

    # ------------------------------------------------------------------
    # Key revisions
    #
    # Provider adapters bake their API key in at construction and are then
    # memoized by ProviderRegistry for the life of the process. Rather than
    # requiring every mutation site to remember to evict that cache — which is
    # how ``/login`` came to be a no-op until restart — the registry pulls the
    # revision below and rebuilds when it moves. Any future write path gets the
    # behaviour for free as long as it goes through this class.
    #
    # Only *key material* bumps the revision. A pure status change
    # (verified/invalid/stale) leaves the cached adapter correct.
    # ------------------------------------------------------------------

    def _bump_revision(self, provider_id: str) -> None:
        """Signal that *provider_id*'s key material changed. Caller holds the lock."""
        self._key_revisions[provider_id] = self._key_revisions.get(provider_id, 0) + 1

    def key_revision(self, provider_id: str) -> int:
        """Monotonic counter for *provider_id*'s key material within this process."""
        with self._cache_lock:
            return self._key_revisions.get(provider_id, 0)


_manager = CredentialManager()


def save_key(provider_id: str, api_key: str, *, verified: bool = False) -> None:
    """Persist *api_key* for *provider_id*.

    ``verified`` must be True only when the provider itself has just accepted
    this key. It defaults to False so that a caller which skipped validation —
    ``--no-validate``, or the "Save anyway" branch taken when the network is
    down — cannot silently record an unchecked key as verified.
    """
    _manager.save_provider(
        provider_id,
        api_key,
        status=_STATUS_VERIFIED if verified else _STATUS_UNVERIFIED,
    )


def key_revision(provider_id: str) -> int:
    """Monotonic counter bumped whenever *provider_id*'s key material changes.

    ``ProviderRegistry`` compares this against the revision it built a cached
    adapter at, so a saved or deleted key takes effect on the next call instead
    of requiring a process restart.
    """
    return _manager.key_revision(provider_id)


def mark_verified(provider_id: str, *, model_count: int = 0) -> None:
    """Record that the provider accepted this key just now."""
    _manager.update_status(provider_id, _STATUS_VERIFIED, model_count=model_count)


def mark_invalid(provider_id: str, *, reason: str = "") -> None:
    """Record that the provider actively rejected this key.

    Reserved for verdicts that say something about the *key* — rejected,
    expired, revoked, forbidden. A network failure or a rate-limit says nothing
    about the key and must not land here; see ``providers/verifier.py``.
    """
    _manager.update_status(provider_id, _STATUS_INVALID, last_error=reason[:200])


def _stored_state(record: dict[str, Any]) -> KeyState:
    """Map an on-disk record to a :class:`KeyState`, honouring the TTL."""
    raw = str(record.get("status", "")).lower()
    status = _LEGACY_STATUS_MAP.get(raw, raw)

    if status == _STATUS_INVALID:
        return KeyState.INVALID
    if status != _STATUS_VERIFIED:
        return KeyState.UNVERIFIED

    stamp = record.get("last_verified")
    if not stamp:
        return KeyState.STALE
    try:
        seen = datetime.fromisoformat(str(stamp))
    except ValueError:
        return KeyState.STALE
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)

    age_ok = datetime.now(timezone.utc) - seen < timedelta(seconds=VERIFY_TTL_SECONDS)
    return KeyState.VERIFIED if age_ok else KeyState.STALE


def verification_state(provider_id: str) -> KeyState:
    """Return the lifecycle state of *provider_id*'s credential.

    The one predicate the UI should ask. Unlike :func:`has_key`, this
    distinguishes a key the provider has accepted from one that was never
    checked, one that has gone stale, and one that has been rejected.

    A key sourced from the environment reports :attr:`KeyState.ENV`: we cannot
    manage a lifecycle we don't own, and re-verifying it on a timer would be
    surprising.
    """
    env_var = PROVIDER_ENV_VARS.get(provider_id)
    if env_var and os.getenv(env_var):
        return KeyState.ENV

    record = _manager.get_provider(provider_id)
    if not record or not record.get("key"):
        return KeyState.MISSING
    return _stored_state(record)


def list_stale_providers() -> list[str]:
    """Provider IDs whose stored key is due a background re-check."""
    return [
        pid for pid in _manager.get_all_providers() if verification_state(pid) is KeyState.STALE
    ]


def list_invalid_providers() -> list[str]:
    """Provider IDs whose stored key the provider has rejected."""
    return [
        pid for pid in _manager.get_all_providers() if verification_state(pid) is KeyState.INVALID
    ]


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


# Short-lived memo for the Ollama liveness probe. The probe is a synchronous
# socket connect, and hot callers (the REPL home surface renders it, provider
# UIs poll it) could otherwise run it many times a second. On a machine with no
# Ollama the connect takes the refused/timeout path — slow on Windows under
# loopback firewall inspection — so a per-call socket would visibly stall the
# UI thread. Memoize the verdict for a few seconds; liveness rarely flips faster.
_OLLAMA_LIVE_TTL = 5.0
_ollama_live_cache: tuple[bool, float] | None = None


def is_ollama_live(timeout: float = 0.05) -> bool:
    """Return True if a local Ollama server is reachable.

    Result is memoized for ``_OLLAMA_LIVE_TTL`` seconds so repeated callers
    (status/home rendering, provider polling) never hammer the socket. The
    ``timeout`` only applies on a cache miss. Callers that need a guaranteed
    fresh reading should probe the daemon directly.

    The default is deliberately tiny: this is called on every process's
    startup path (``onboarding_state`` -> ``list_configured_providers``), and
    when nothing is listening, Windows' loopback stack can take the better
    part of a quarter-second to refuse the connection — a live daemon,
    in contrast, accepts in low single-digit milliseconds, so a short timeout
    costs nothing in the common "Ollama running" case while capping the
    worst-case "Ollama not installed" stall. Callers that need more headroom
    against a slow-to-accept daemon (status displays, explicit /providers
    checks) already pass their own larger ``timeout``.
    """
    global _ollama_live_cache

    import time as _time

    now = _time.monotonic()
    cached = _ollama_live_cache
    if cached is not None and (now - cached[1]) < _OLLAMA_LIVE_TTL:
        return cached[0]

    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=timeout):
            live = True
    except OSError:
        live = False

    _ollama_live_cache = (live, now)
    return live


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
            "status": str(KeyState.ENV),
            "last_verified": "dynamic",
            "location": f"${env_var}",
        }

    if record:
        return {
            "stored": True,
            "source": "config",
            "status": str(verification_state(provider_id)),
            "last_verified": record.get("last_verified", "never"),
            "last_error": record.get("last_error", ""),
            "location": str(_manager._credentials_file),
        }

    return {
        "stored": False,
        "source": "none",
        "status": str(KeyState.MISSING),
        "last_verified": "n/a",
        "location": "n/a",
    }


def credentials_file_path() -> Path:
    """Return the path to the encrypted credentials file."""
    return _manager._credentials_file


def export_provider_metadata() -> dict[str, Any]:
    """Produce a secrets-free snapshot of all configured providers.

    Every "key" field is the fixed literal ``"***"`` — this function contains
    no expression anywhere that reads a real stored or env-sourced key value,
    so its return value is safe to serialise to a plaintext file or log. This
    is the only export path :func:`velune.recovery.archive._backup_providers`
    uses for the unencrypted ``providers.json`` and for the backup manifest's
    provider-id summary, so neither can ever carry secret material — not just
    "in practice", but because the read never happens in this call graph.
    """
    snap: dict[str, Any] = {}

    for pid, rec in _manager.get_all_providers().items():
        snap[pid] = {
            "key": "***",
            "status": rec.get("status", "unknown"),
            "last_verified": rec.get("last_verified", ""),
            "source": "file",
        }

    for pid, env_var in PROVIDER_ENV_VARS.items():
        if pid not in snap and os.getenv(env_var):
            snap[pid] = {
                "key": "***",
                "status": "env",
                "last_verified": "dynamic",
                "source": "environment",
                "env_var": env_var,
            }

    return snap


def export_provider_secrets() -> dict[str, Any]:
    """Produce a snapshot of all configured providers *including* real keys.

    Callers must encrypt the return value before writing it anywhere and must
    never merge it with data that reaches a plaintext sink — this function
    exists precisely so those two concerns stay in separate call graphs.
    Used only by :func:`velune.recovery.archive._backup_providers`'s
    ``with_secrets=True`` path, which AES-GCM-encrypts the result immediately.
    """
    snap: dict[str, Any] = {}

    for pid, rec in _manager.get_all_providers().items():
        snap[pid] = {
            "key": rec.get("key", ""),
            "status": rec.get("status", "unknown"),
            "last_verified": rec.get("last_verified", ""),
            "source": "file",
        }

    for pid, env_var in PROVIDER_ENV_VARS.items():
        if pid not in snap:
            val = os.getenv(env_var)
            if val:
                snap[pid] = {
                    "key": val,
                    "status": "env",
                    "last_verified": "dynamic",
                    "source": "environment",
                    "env_var": env_var,
                }

    return snap


def export_providers_json(include_keys: bool = True) -> dict[str, Any]:
    """Produce a serialisable snapshot of all configured providers.

    Kept for backward compatibility with existing callers (e.g. ``velune
    provider backup``). New code should call :func:`export_provider_metadata`
    or :func:`export_provider_secrets` directly instead of branching on a
    boolean here — that keeps the secret-reading and secret-free code paths
    statically distinguishable, which is what lets static analysis prove a
    given call site can never observe real key material.
    """
    return export_provider_secrets() if include_keys else export_provider_metadata()


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
        # An imported key has not been checked against the provider *by us*,
        # whatever the archive claims, so it enters as UNVERIFIED and the
        # background re-verifier picks it up.
        _manager.save_provider(pid, key, status=_STATUS_UNVERIFIED)
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
