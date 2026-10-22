"""Authoritative API key storage layer.

Provides a unified, cross-platform, encrypted credential manager backed by a
single JSON configuration file. Supports atomic file writes to prevent corruption
and merges updates rather than overwriting unrelated providers.

Persistence guarantees
----------------------
* Every write is a read-modify-write of the *current* on-disk state, under a
  cross-process file lock, so saving one provider never reverts or drops
  another provider's record — including one written by a different terminal.
* A store that exists but cannot be read (wrong master key, truncated file) is
  never overwritten. Reads degrade to "no stored keys" so the CLI still runs on
  environment variables; writes raise :class:`CredentialStoreError` instead of
  silently replacing every saved key with the one being added.
* After each write the file is decrypted again and compared with what was
  meant to be written; a mismatch raises rather than reporting success.
* The in-memory cache is re-read whenever the file changes on disk, so a key
  saved by another process is seen without a restart.

Key precedence
--------------
A key saved through Velune (``/connect``, onboarding, ``velune provider add``)
is the key Velune uses. A provider environment variable is used when nothing is
stored for that provider. Set ``VELUNE_PREFER_ENV_KEYS=1`` to make environment
variables win instead (for CI or a shell that deliberately overrides a saved
key). :func:`key_source` reports which one is in effect, without the value.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from platformdirs import user_config_dir

from velune._compat import StrEnum
from velune.core.filelock import FileLockTimeout, locked
from velune.providers.crypto import (
    DecryptionError,
    MasterKeyUnavailable,
    decrypt_credentials,
    encrypt_credentials,
    keyring_answered,
)

logger = logging.getLogger("velune.providers.keystore")

# How long a successful verification is trusted before the key is considered
# STALE and re-checked in the background. Stale never means unusable — see
# KeyState below.
VERIFY_TTL_SECONDS = 86_400  # 24h

# How soon an unreadable store is retried on the next lookup (see _ensure_cache).
_LOAD_RETRY_SECONDS = 2.0


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


class _EmptyStoreFile(ValueError):
    """A store file exists but holds no bytes at all."""


class CredentialStoreError(OSError):
    """The encrypted credential store could not be read or written safely.

    Raised instead of reporting a save as successful, and instead of replacing
    an unreadable store (whose keys may still be recoverable) with a new one.
    """


class CredentialKeyUnavailable(CredentialStoreError):
    """The store looks intact but its key cannot be reached right now.

    Distinct from a damaged store: the fix is to unlock or restore the keyring
    (or set the same passphrase), not to move any file. ``repair`` refuses it.
    """


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

#: The primary environment variable for each provider (what docs and error
#: messages name).
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

#: Secondary names accepted after the primary one. NVIDIA's own docs use both:
#: ``NVIDIA_API_KEY`` for the hosted API and ``NGC_API_KEY`` for NIM containers.
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "nvidia": ("NGC_API_KEY",),
}

#: Opt-in: environment variables take precedence over saved keys.
PREFER_ENV_VAR = "VELUNE_PREFER_ENV_KEYS"


def provider_env_vars(provider_id: str) -> tuple[str, ...]:
    """Every environment variable read for *provider_id*, in priority order."""
    primary = PROVIDER_ENV_VARS.get(provider_id)
    if primary is None:
        return ()
    return (primary, *_ENV_ALIASES.get(provider_id, ()))


def all_provider_env_vars() -> list[str]:
    """Every provider environment variable name (primary and aliases)."""
    names: list[str] = []
    for pid in PROVIDER_ENV_VARS:
        names.extend(provider_env_vars(pid))
    return names


def _env_key(provider_id: str) -> tuple[str, str] | None:
    """``(env_var_name, value)`` of the first non-empty provider env var.

    Values are stripped: a key exported with a trailing newline or space used to
    fail the "no whitespace" structural check and get the provider marked
    invalid in the background.
    """
    for name in provider_env_vars(provider_id):
        value = os.getenv(name, "").strip()
        if value:
            return name, value
    return None


def _prefer_env() -> bool:
    return os.getenv(PREFER_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def _restrict_to_current_user_windows(path: Path) -> None:
    """Best-effort Windows equivalent of ``chmod 600`` for *path*.

    ``Path.chmod`` is a no-op for real ACL enforcement on Windows, so the
    encrypted credential file otherwise keeps whatever (often broader) ACL
    it inherited from its parent directory. This strips inherited entries
    and grants Full Control to the current user only, via ``icacls`` (no
    extra dependency needed). Never allowed to block a credential save: a
    failure here — e.g. ``icacls`` unavailable, or a filesystem without ACL
    support — just leaves the file at its default permissions.
    """
    import subprocess

    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    if not user:
        return
    account = f"{domain}\\{user}" if domain else user
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
            shell=False,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        logger.debug("Could not restrict ACL on %s (non-fatal)", path, exc_info=True)


def _restrict(path: Path) -> None:
    if not path.exists():
        return
    if os.name != "nt":
        path.chmod(0o600)
    else:
        _restrict_to_current_user_windows(path)


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
        # (mtime_ns, size) of the file the cache was loaded from; a change means
        # another process wrote it and the cache must be re-read.
        self._cache_stamp: tuple[int, int] | None = None
        self._cache_lock = Lock()
        self._key_revisions: dict[str, int] = {}
        # Why the last load could not read the store, for diagnostics.
        self.load_error: str | None = None
        self.load_key_unavailable = False
        self._load_failed_at: float | None = None

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def _backup_file(self) -> Path:
        return self._credentials_file.with_name("credentials.json.bak")

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self._credentials_file.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    @staticmethod
    def _decode(path: Path) -> dict[str, dict[str, Any]]:
        """Decrypt and parse one store file; raises on anything unreadable."""
        encrypted_data = path.read_bytes()
        if not encrypted_data:
            raise _EmptyStoreFile(f"{path.name} is empty")
        data = json.loads(decrypt_credentials(encrypted_data))
        providers = data.get("providers", {}) if isinstance(data, dict) else None
        if not isinstance(providers, dict):
            raise ValueError("credentials file has no provider table")
        return providers

    def _read_store(self) -> tuple[dict[str, dict[str, Any]], str]:
        """Return ``(providers, source)``; source is primary/backup/empty.

        Falls back to ``credentials.json.bak`` when the primary is unreadable,
        reading it in place — the old code copied the backup over the primary
        *before* checking it decrypted, destroying the only other copy of an
        undecryptable-but-recoverable store.

        Raises :class:`CredentialStoreError` when a store exists and neither
        file can be read.
        """
        if not self._credentials_file.exists():
            return {}, "empty"
        try:
            return self._decode(self._credentials_file), "primary"
        except _EmptyStoreFile as primary_exc:
            # Velune never writes an empty store (every save is an encrypted
            # table), so a zero-byte file is damage — a crash or a full disk —
            # not "no keys". Read the backup instead; with no backup there is
            # nothing to lose and it counts as a fresh store. Reporting it as
            # "primary" would copy the empty file over the backup on the next
            # save.
            primary_error = primary_exc
            if not self._backup_file.exists():
                return {}, "empty"
        except Exception as primary_exc:
            primary_error = primary_exc
        if self._backup_file.exists():
            try:
                data = self._decode(self._backup_file)
                logger.warning(
                    "credentials.json could not be read (%s); using credentials.json.bak.",
                    type(primary_error).__name__,
                )
                return data, "backup"
            except Exception:
                pass
        # A decryption failure while the keyring is not answering says nothing
        # about the file: the key is simply out of reach. Anything else (the
        # keyring answered and its key still does not open the file, or the
        # decrypted content is not a credential table) means the store itself
        # cannot be used with the keys we have.
        if isinstance(primary_error, DecryptionError) and not keyring_answered():
            raise CredentialKeyUnavailable(
                "Your saved keys are intact but locked: the OS keyring is not "
                "answering. Unlock or restore the keyring (or set the same "
                "VELUNE_MASTER_PASSPHRASE as before); Velune picks the keys up "
                "again on its own, no restart needed. Nothing was changed and no "
                f"new key was created. Do not run `velune provider repair` for this. File: {self._credentials_file}"
            )
        raise CredentialStoreError(
            f"Stored credentials could not be read with the keys available ({primary_error}). "
            f"Nothing was overwritten: the file and its backup are untouched at "
            f"{self._credentials_file}. If you use VELUNE_MASTER_PASSPHRASE, check it is "
            "set to the value used when the keys were saved. Otherwise run "
            "`velune provider repair`: it restores a readable backup if there is one, "
            "or moves this file aside (not deleted) so you can add your keys again. "
            "It cannot recover keys that were encrypted with a key you no longer have."
        )

    def _load_disk(self, retry: bool = True) -> dict[str, dict[str, Any]]:
        """Load the store for reading. Unreadable → ``{}`` plus :attr:`load_error`.

        ``retry`` is accepted for backward compatibility; the backup fallback
        is now always attempted (read-only) by :meth:`_read_store`.
        """
        try:
            data, _source = self._read_store()
            self.load_error = None
            self.load_key_unavailable = False
            return data
        except CredentialStoreError as exc:
            # Non-fatal for reads: the CLI keeps working on environment
            # variables. Writes go through _read_store directly and refuse.
            self.load_error = str(exc)
            self.load_key_unavailable = isinstance(exc, CredentialKeyUnavailable)
            logger.warning("%s", exc)
            return {}

    def _ensure_cache(self) -> dict[str, dict[str, Any]]:
        """The cached provider table, re-read if the file changed. Caller holds the lock."""
        stamp = self._stamp()
        # A load that failed (keyring locked, wrong passphrase) is retried after
        # a short pause even though the file itself has not changed: fixing the
        # cause happens outside the file, and a long-running REPL must not stay
        # blind to its saved keys until restart. The pause keeps a persistent
        # failure from re-running the key derivation on every lookup.
        retry_failed = (
            self.load_error is not None
            and self._load_failed_at is not None
            and time.monotonic() - self._load_failed_at >= _LOAD_RETRY_SECONDS
        )
        if self._cache is None or stamp != self._cache_stamp or retry_failed:
            self._cache = self._load_disk()
            self._cache_stamp = stamp
            self._load_failed_at = time.monotonic() if self.load_error is not None else None
        return self._cache

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold the cross-process lock; a timeout becomes a :class:`CredentialStoreError`.

        Every caller handles that error type, whereas a bare ``FileLockTimeout``
        would surface as a crash instead of "try again".
        """
        try:
            with locked(self._credentials_file):
                yield
        except FileLockTimeout as exc:
            raise CredentialStoreError(
                "Another Velune process is saving credentials right now. "
                "Nothing was changed; try again in a moment."
            ) from exc

    def _transact(
        self, mutate: Callable[[dict[str, dict[str, Any]]], None]
    ) -> dict[str, dict[str, Any]]:
        """Apply *mutate* to the current on-disk state and write it back.

        The read-modify-write runs under a cross-process
        :func:`~velune.core.filelock.locked` guard: this class's own
        ``_cache_lock`` only protects against races between threads in *this*
        process, so two terminal sessions saving keys at the same moment could
        otherwise both read the same state and the second write would discard
        the first one's update. *mutate* receives a copy of the freshly read
        state, so only the records it touches change.

        Raises :class:`CredentialStoreError` if the existing store cannot be
        read (it is left untouched), if the write fails, or if reading the
        written file back does not return exactly what was written.
        """
        self._config_dir.mkdir(parents=True, exist_ok=True)

        with self._exclusive():
            current, source = self._read_store()
            updated = copy.deepcopy(current)
            mutate(updated)

            try:
                encrypted_data = encrypt_credentials(
                    json.dumps({"providers": updated}, indent=2), protect_store=True
                )
            except MasterKeyUnavailable as exc:
                raise CredentialStoreError(str(exc)) from exc

            # Atomic write sequence: temp file -> fsync -> rename.
            temp_file = self._credentials_file.with_suffix(".tmp")
            try:
                with open(temp_file, "wb") as f:
                    f.write(encrypted_data)
                    f.flush()
                    os.fsync(f.fileno())

                if source == "primary":
                    # Keep the last known-good state. Only a primary that just
                    # decrypted is copied — never an unreadable one, which would
                    # overwrite the backup this write is being made from.
                    shutil.copy2(self._credentials_file, self._backup_file)
                elif source == "backup" and self._credentials_file.exists():
                    # Preserve the unreadable primary for manual recovery
                    # rather than replacing it outright.
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    shutil.copy2(
                        self._credentials_file,
                        self._credentials_file.with_name(f"credentials.json.unreadable-{stamp}"),
                    )

                temp_file.replace(self._credentials_file)

                # Restrict the encrypted store (and its backup) to the current
                # user only, so it isn't world-readable on a shared machine.
                _restrict(self._credentials_file)
                _restrict(self._backup_file)
            except OSError as exc:
                raise CredentialStoreError(
                    f"Could not write {self._credentials_file}: {exc}"
                ) from exc
            finally:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except OSError:
                        pass

            try:
                written = self._decode(self._credentials_file)
            except Exception as exc:
                raise CredentialStoreError(
                    f"Saved credentials could not be read back: {exc}"
                ) from exc
            if written != updated:
                raise CredentialStoreError(
                    "Saved credentials did not match what was written; the save was not confirmed."
                )

            return updated

    def _commit(self, mutate: Callable[[dict[str, dict[str, Any]]], None]) -> None:
        """Run :meth:`_transact` and adopt the result as the cache. Caller holds the lock."""
        self._cache = self._transact(mutate)
        self._cache_stamp = self._stamp()
        self.load_error = None

    def _save_disk(
        self, providers: dict[str, dict[str, Any]], *, removed: set[str] | None = None
    ) -> None:
        """Merge *providers* onto the on-disk state and drop *removed* (compat helper)."""

        def _apply(state: dict[str, dict[str, Any]]) -> None:
            state.update(providers)
            for provider_id in removed or ():
                state.pop(provider_id, None)

        self._transact(_apply)

    def _attempt_backup_restore(self) -> bool:
        """Copy a *readable* backup over the primary, keeping the primary aside.

        Used by :func:`repair_keystore`. Reads never call this any more — they
        read the backup in place.
        """
        backup = self._backup_file
        if not backup.exists():
            return False
        try:
            self._decode(backup)
        except Exception as e:
            logger.error("Backup credentials are unreadable too: %s", e)
            return False
        try:
            if self._credentials_file.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                shutil.copy2(
                    self._credentials_file,
                    self._credentials_file.with_name(f"credentials.json.unreadable-{stamp}"),
                )
            shutil.copy2(backup, self._credentials_file)
            logger.info("Restored credentials from backup.")
            return True
        except Exception as e:
            logger.error("Failed to restore backup: %s", e)
            return False

    # ------------------------------------------------------------------
    # Public record API
    # ------------------------------------------------------------------

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        """Get the full record for a provider."""
        with self._cache_lock:
            return self._ensure_cache().get(provider_id)

    def save_provider(self, provider_id: str, key: str, status: str = _STATUS_UNVERIFIED) -> None:
        """Save a provider's key and its verification *status*.

        ``status`` defaults to UNVERIFIED, not VERIFIED: a caller that has not
        actually seen the provider accept this key must not be able to record it
        as verified by omission. Callers that *have* validated pass
        ``_STATUS_VERIFIED`` (or go through :func:`mark_verified`).

        ``last_verified`` is only stamped for a verified save — for an
        unverified or invalid one there is no verification to date, and writing
        "now" there would make a never-checked key look freshly checked.

        Raises :class:`CredentialStoreError` when the key could not be saved and
        confirmed; the previously stored state is left intact in that case.
        """
        # Accept the legacy spellings ("valid"/"imported") so an older caller or
        # a restored backup lands in the right state rather than silently
        # skipping the timestamp below and reading back as STALE forever.
        status = _LEGACY_STATUS_MAP.get(status, status)

        record: dict[str, Any] = {"key": key, "status": status}
        if status == _STATUS_VERIFIED:
            record["last_verified"] = datetime.now(timezone.utc).isoformat()

        def _apply(state: dict[str, dict[str, Any]]) -> None:
            state[provider_id] = record

        with self._cache_lock:
            self._commit(_apply)
            self._bump_revision(provider_id)

    def update_status(self, provider_id: str, status: str, **extra: Any) -> None:
        """Rewrite a provider's verification status without touching its key.

        Applied to the freshly read on-disk record, so a key replaced by another
        process in the meantime is not reverted to the old one here.
        """

        def _apply(state: dict[str, dict[str, Any]]) -> None:
            record = state.get(provider_id)
            if record is None:
                return
            record = dict(record)
            record["status"] = status
            record.pop("last_error", None)
            if status == _STATUS_VERIFIED:
                record["last_verified"] = datetime.now(timezone.utc).isoformat()
            record.update(extra)
            state[provider_id] = record

        with self._cache_lock:
            if provider_id not in self._ensure_cache():
                return
            self._commit(_apply)

    def delete_provider(self, provider_id: str) -> None:
        """Delete a provider from the configuration."""
        with self._cache_lock:
            if provider_id not in self._ensure_cache():
                return
            self._commit(lambda state: state.pop(provider_id, None))
            self._bump_revision(provider_id)

    def get_all_providers(self) -> dict[str, dict[str, Any]]:
        """Get a copy of all loaded providers."""
        with self._cache_lock:
            return dict(self._ensure_cache())

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

    Raises :class:`CredentialStoreError` if the key could not be persisted.
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


# How often a key the provider rejected is re-checked in the background. A
# rejection is usually final, but INVALID used to be written for 403s
# (permission on one model) as well, and a provider-side fix (key re-enabled,
# billing restored) should not require deleting and re-adding the key.
INVALID_RECHECK_SECONDS = 6 * 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_verified(provider_id: str, *, model_count: int = 0) -> None:
    """Record that the provider accepted this key just now."""
    _manager.update_status(
        provider_id, _STATUS_VERIFIED, model_count=model_count, last_checked=_now_iso()
    )


def mark_invalid(provider_id: str, *, reason: str = "") -> None:
    """Record that the provider actively rejected this key.

    Reserved for verdicts that say something about the *key* — rejected,
    expired, revoked. A network failure, a rate limit, a missing model or a
    permission error on one endpoint says nothing about the key and must not
    land here; see ``providers/verifier.py``.
    """
    _manager.update_status(
        provider_id, _STATUS_INVALID, last_error=reason[:200], last_checked=_now_iso()
    )


def note_check_failure(provider_id: str, *, reason: str) -> None:
    """Remember why the last check was inconclusive, without changing state."""
    record = _manager.get_provider(provider_id)
    if record is None:
        return
    raw = str(record.get("status", _STATUS_UNVERIFIED))
    status = _LEGACY_STATUS_MAP.get(raw, raw)
    extra: dict[str, Any] = {"last_check_error": reason[:200], "last_checked": _now_iso()}
    if status == _STATUS_VERIFIED and record.get("last_verified"):
        # update_status re-stamps last_verified for VERIFIED; keep the old stamp
        # so an inconclusive check doesn't extend the TTL.
        extra["last_verified"] = record["last_verified"]
    if status == _STATUS_INVALID and record.get("last_error"):
        extra["last_error"] = record["last_error"]
    _manager.update_status(provider_id, status, **extra)


def _invalid_recheck_due(record: dict[str, Any]) -> bool:
    stamp = record.get("last_checked")
    if not stamp:
        return True
    try:
        seen = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - seen >= timedelta(seconds=INVALID_RECHECK_SECONDS)


def list_recheck_due_providers() -> list[str]:
    """Stored keys due a background check: STALE ones, and INVALID ones not checked recently."""
    due: list[str] = []
    for pid, record in _manager.get_all_providers().items():
        state = verification_state(pid)
        if state is KeyState.STALE or (state is KeyState.INVALID and _invalid_recheck_due(record)):
            due.append(pid)
    return due


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


@dataclass(frozen=True, slots=True)
class KeySource:
    """Which credential is in effect for a provider — never the value itself."""

    provider_id: str
    source: str  # "stored" | "environment" | "none"
    env_var: str | None = None  # the env var in effect, when source == "environment"
    shadowed_env_var: str | None = None  # a set env var that a stored key overrides
    shadowed_stored: bool = False  # a stored key that an env var overrides

    def describe(self) -> str:
        if self.source == "stored":
            note = f" (overrides ${self.shadowed_env_var})" if self.shadowed_env_var else ""
            return f"saved key{note}"
        if self.source == "environment":
            note = " (overrides the saved key)" if self.shadowed_stored else ""
            return f"${self.env_var}{note}"
        return "not configured"


def key_source(provider_id: str) -> KeySource:
    """Report where *provider_id*'s effective key comes from (see module docstring)."""
    env = _env_key(provider_id)
    record = _manager.get_provider(provider_id)
    stored = bool(record and record.get("key"))

    if env and (_prefer_env() or not stored):
        return KeySource(provider_id, "environment", env_var=env[0], shadowed_stored=stored)
    if stored:
        return KeySource(provider_id, "stored", shadowed_env_var=env[0] if env else None)
    return KeySource(provider_id, "none")


def override_note(provider_id: str) -> str | None:
    """A one-line explanation when a saved key and an env var are both present."""
    src = key_source(provider_id)
    if src.source == "stored" and src.shadowed_env_var:
        return (
            f"${src.shadowed_env_var} is also set; Velune uses the saved key. "
            f"Set {PREFER_ENV_VAR}=1 to prefer the environment variable instead."
        )
    if src.source == "environment" and src.shadowed_stored:
        return f"{PREFER_ENV_VAR} is set, so ${src.env_var} is used instead of the saved key."
    return None


def verification_state(provider_id: str) -> KeyState:
    """Return the lifecycle state of *provider_id*'s credential.

    The one predicate the UI should ask. Unlike :func:`has_key`, this
    distinguishes a key the provider has accepted from one that was never
    checked, one that has gone stale, and one that has been rejected.

    A key sourced from the environment reports :attr:`KeyState.ENV`: we cannot
    manage a lifecycle we don't own, and re-verifying it on a timer would be
    surprising. It follows the same precedence as :func:`get_key`, so a saved
    key that overrides an environment variable reports its own state.
    """
    src = key_source(provider_id)
    if src.source == "environment":
        return KeyState.ENV
    if src.source == "none":
        return KeyState.MISSING
    record = _manager.get_provider(provider_id) or {}
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
    """Return the effective API key for *provider_id* (see "Key precedence")."""
    env = _env_key(provider_id)
    if env and _prefer_env():
        return env[1]

    record = _manager.get_provider(provider_id)
    if record and "key" in record and (record["key"] or not env):
        return record["key"]
    if env:
        return env[1]
    return None


def delete_key(provider_id: str) -> None:
    """Remove the stored key for *provider_id*."""
    _manager.delete_provider(provider_id)


def has_key(provider_id: str) -> bool:
    """Return True if a key is available for *provider_id*."""
    return get_key(provider_id) is not None


def store_load_error() -> str | None:
    """Why the credential store could not be read on the last load, if it couldn't."""
    with _manager._cache_lock:
        _manager._ensure_cache()
    return _manager.load_error


# Short-lived memo for the Ollama liveness probe. The probe is a synchronous
# socket connect, and hot callers (the REPL home surface renders it, provider
# UIs poll it) could otherwise run it many times a second. On a machine with no
# Ollama the connect takes the refused/timeout path — slow on Windows under
# loopback firewall inspection — so a per-call socket would visibly stall the
# UI thread. Memoize the verdict for a few seconds; liveness rarely flips faster.
_OLLAMA_LIVE_TTL = 5.0
_ollama_live_cache: tuple[bool, float, tuple[str, int]] | None = None


def is_ollama_live(timeout: float = 0.05) -> bool:
    """Return True if the configured Ollama server is reachable.

    Probes the endpoint from :func:`velune.providers.endpoints.ollama_base_url`
    (``OLLAMA_HOST`` / config aware), not a hardcoded ``localhost:11434``.

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

    from velune.providers.endpoints import ollama_socket_address

    address = ollama_socket_address()
    now = _time.monotonic()
    cached = _ollama_live_cache
    if cached is not None and cached[2] == address and (now - cached[1]) < _OLLAMA_LIVE_TTL:
        return cached[0]

    import socket

    try:
        with socket.create_connection(address, timeout=timeout):
            live = True
    except OSError:
        live = False

    _ollama_live_cache = (live, now, address)
    return live


def list_configured_providers(include_ollama: bool = True) -> list[str]:
    """Return provider IDs that have a usable key.

    Environment variables are checked first, followed by the local JSON configuration.
    """
    configured: set[str] = set()

    for pid in PROVIDER_ENV_VARS:
        if _env_key(pid):
            configured.add(pid)

    disk_providers = _manager.get_all_providers()
    for pid in disk_providers:
        configured.add(pid)

    ordered = [pid for pid in PROVIDER_ENV_VARS if pid in configured]

    if include_ollama and is_ollama_live():
        ordered.insert(0, "ollama")
    return ordered


def get_provider_status(provider_id: str) -> dict[str, Any]:
    """Diagnostic helper for returning a provider's state and configuration info.

    Reports the effective credential *source* (saved key or which environment
    variable) and what it overrides — never the key itself.
    """
    record = _manager.get_provider(provider_id)
    src = key_source(provider_id)

    if src.source == "environment":
        return {
            "stored": True,
            "source": "environment",
            "status": str(KeyState.ENV),
            "last_verified": "dynamic",
            "location": f"${src.env_var}",
            "effective": src.describe(),
        }

    if record:
        status: dict[str, Any] = {
            "stored": True,
            "source": "config",
            "status": str(verification_state(provider_id)),
            "last_verified": record.get("last_verified", "never"),
            "last_error": record.get("last_error", ""),
            "last_check_error": record.get("last_check_error", ""),
            "location": str(_manager._credentials_file),
            "effective": src.describe(),
        }
        return status

    return {
        "stored": False,
        "source": "none",
        "status": str(KeyState.MISSING),
        "last_verified": "n/a",
        "location": "n/a",
        "effective": src.describe(),
        "store_error": _manager.load_error or "",
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
    1. Force a reload from disk.
    2. If the store cannot be read, restore a *readable* backup; failing that,
       move the unreadable file aside (``credentials.json.unreadable-<time>``)
       so a fresh store can be written. Nothing is deleted.
    3. Remove any records that have an empty key.

    Returns a report dict with keys ``restored_backup``, ``quarantined``,
    ``removed``, ``kept``.
    """
    report: dict[str, Any] = {
        "restored_backup": False,
        "quarantined": "",
        "removed": [],
        "kept": [],
    }

    with _manager._cache_lock:
        _manager._cache = None
        _manager._ensure_cache()
        unreadable = _manager.load_error is not None
        key_unavailable = _manager.load_key_unavailable

    if unreadable and key_unavailable:
        # The file is fine; the keyring is not answering. Moving it aside or
        # restoring a backup over it would turn a temporary lock into a loss.
        report["blocked"] = _manager.load_error
        return report

    if unreadable:
        if _manager._attempt_backup_restore():
            report["restored_backup"] = True
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = _manager._credentials_file.with_name(f"credentials.json.unreadable-{stamp}")
            try:
                _manager._credentials_file.replace(target)
                report["quarantined"] = str(target)
            except OSError as exc:
                logger.error("Could not move the unreadable store aside: %s", exc)
        with _manager._cache_lock:
            _manager._cache = None

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
