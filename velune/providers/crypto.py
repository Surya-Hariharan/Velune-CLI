"""Encryption layer for Velune's persistent credentials.

This module provides AES-GCM encryption for the credentials.json file.
It attempts to use the OS keyring (Credential Manager / Keychain) to store
a randomly generated 256-bit master key. If the keyring is unavailable, it
prefers a key derived from the ``VELUNE_MASTER_PASSPHRASE`` environment
variable (PBKDF2-HMAC-SHA256) — suitable for headless servers, Docker, and
CI — and only as a last resort falls back to a weak machine-derived key so
credentials are never written in the clear.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from platformdirs import user_config_dir

logger = logging.getLogger("velune.providers.crypto")

_SERVICE = "velune"
_USERNAME = "master_key"

_PBKDF2_ITERATIONS = 390_000
_SALT_LEN = 16
_NONCE_LEN = 12

#: Optional env var holding a user secret used to derive the master key when no
#: OS keyring is available (headless servers, Docker, CI). Vastly stronger than
#: the machine-derived fallback and portable across machines.
_ENV_PASSPHRASE = "VELUNE_MASTER_PASSPHRASE"

#: Versioned static salt for the passphrase-derived *master* key. Static (not
#: random) so the same passphrase yields the same key across runs without
#: persisting a salt beside the ciphertext — this keeps the on-disk
#: ``nonce + ciphertext`` format unchanged. PBKDF2 at the iteration count above
#: over a real user secret is still far stronger than the machine key.
_MASTER_PASSPHRASE_SALT = b"velune_master_passphrase_salt_v1"

#: Guard so the "weak at-rest protection" warning is emitted at most once.
_warned_no_protection = False


class DecryptionError(Exception):
    """Raised when passphrase-based decryption fails (wrong passphrase or corrupt data)."""


def _get_machine_id() -> str:
    """Generate a machine-specific ID as a fallback."""
    try:
        # Get stable hardware node ID
        node = uuid.getnode()
        return str(node)
    except Exception:
        return "fallback_machine_id"


def _get_fallback_key() -> bytes:
    """Derive a stable 256-bit machine key — last-resort fallback only.

    Weak by design (the machine id is not secret); used only when neither an OS
    keyring nor a ``VELUNE_MASTER_PASSPHRASE`` is available, purely so
    credentials are not written in the clear. Retained unchanged so stores
    previously encrypted under this key remain decryptable.
    """
    machine_id = _get_machine_id().encode("utf-8")
    salt = b"velune_fallback_salt_v1"

    hasher = hashlib.sha256()
    hasher.update(machine_id)
    hasher.update(salt)
    return hasher.digest()


def _passphrase_file_path() -> Path:
    """Where a passphrase entered at the first-run prompt is persisted.

    Only the passphrase's *hash-derived* consumption differs from the env var
    — the passphrase text itself is stored here so the user is asked exactly
    once rather than every process start. File permissions (0600 / a
    current-user-only Windows ACL) are the security boundary, same tier as
    the credentials file itself — strictly better than the machine-derived
    fallback this replaces, though not as strong as a real OS keyring.
    """
    return Path(user_config_dir("Velune")) / "master.passphrase"


def _stored_passphrase() -> str | None:
    """Passphrase persisted by a previous first-run prompt, if any."""
    try:
        text = _passphrase_file_path().read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def _persist_passphrase(passphrase: str) -> None:
    """Save *passphrase* so future processes don't re-prompt for it."""
    path = _passphrase_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(passphrase, encoding="utf-8")
        if os.name == "nt":
            _restrict_passphrase_file_windows(path)
        else:
            path.chmod(0o600)
    except OSError as e:
        logger.debug("Could not persist master passphrase (non-fatal): %s", e)


def _restrict_passphrase_file_windows(path: Path) -> None:
    """Best-effort Windows ACL restriction, mirroring keystore.py's credential guard."""
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


def _credentials_file_exists() -> bool:
    return (Path(user_config_dir("Velune")) / "credentials.json").exists()


def _prompt_for_passphrase() -> str | None:
    """Ask the user, once, to set a master passphrase.

    Only runs when stdin is a real terminal — never in tests, CI, or a
    piped/one-shot invocation, where blocking on input would hang the
    process. Any failure (no tty despite the check, Ctrl-C, EOF) is treated
    as "skip" rather than propagated, since declining just means falling
    back to the existing weak machine-derived key.
    """
    if not sys.stdin.isatty():
        return None
    try:
        import getpass

        print(
            "\nNo OS keyring is available on this system, so Velune cannot "
            "auto-generate a secure encryption key for your stored API keys.\n"
            "Set a passphrase now for stronger at-rest protection than the "
            "machine-derived fallback (press Enter to skip).",
            file=sys.stderr,
        )
        passphrase = getpass.getpass("Master passphrase (blank to skip): ")
        return passphrase.strip() or None
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception as e:
        logger.debug("Passphrase prompt failed (non-fatal): %s", e)
        return None


def _passphrase_master_key() -> bytes | None:
    """Master key derived from a configured passphrase, or None if unset.

    Checks ``VELUNE_MASTER_PASSPHRASE`` first (explicit, session-scoped),
    then a passphrase persisted by a previous first-run prompt.
    """
    passphrase = os.environ.get(_ENV_PASSPHRASE) or _stored_passphrase()
    if not passphrase:
        return None
    return _derive_key_from_passphrase(passphrase, _MASTER_PASSPHRASE_SALT)


def _keyring_read_key() -> bytes | None:
    """Read the stored master key from the OS keyring without creating one.

    Returns None when no backend is available *or* the read fails transiently —
    the caller must not treat that as 'switch keys', which is why decryption
    tries every candidate key rather than a single one.
    """
    try:
        import keyring
    except Exception:
        return None
    try:
        key_b64 = keyring.get_password(_SERVICE, _USERNAME)
    except Exception as e:
        logger.debug("Keyring read failed (transient or unavailable): %s", e)
        return None
    if key_b64:
        return base64.b64decode(key_b64.encode("ascii"))
    return None


def _keyring_create_key() -> bytes | None:
    """Generate and store a new random master key in the keyring; None if it can't."""
    try:
        import keyring

        new_key = AESGCM.generate_key(bit_length=256)
        keyring.set_password(_SERVICE, _USERNAME, base64.b64encode(new_key).decode("ascii"))
        return new_key
    except Exception as e:
        logger.debug("Keyring write failed: %s", e)
        return None


def get_or_create_master_key() -> bytes:
    """Return the master key, preferring the strongest source available.

    Order: existing keyring key → newly created keyring key → passphrase-derived
    key (``VELUNE_MASTER_PASSPHRASE``) → machine fallback. The keyring path is
    unchanged from before; the passphrase step replaces silently trusting the
    weak machine key on keyring-less hosts, and a one-time warning is emitted
    when only the machine fallback remains.
    """
    key = _keyring_read_key()
    if key is not None:
        return key
    key = _keyring_create_key()
    if key is not None:
        return key

    pk = _passphrase_master_key()
    if pk is not None:
        return pk

    # First run (no credentials store yet), no keyring, no passphrase
    # configured anywhere: give the user one interactive chance to opt into
    # passphrase-based encryption before quietly falling back to the weak
    # machine-derived key for the rest of this store's lifetime. Silently
    # skipped outside a real terminal (tests, CI, scripted/one-shot runs).
    if not _credentials_file_exists():
        chosen = _prompt_for_passphrase()
        if chosen:
            _persist_passphrase(chosen)
            return _derive_key_from_passphrase(chosen, _MASTER_PASSPHRASE_SALT)

    global _warned_no_protection
    if not _warned_no_protection:
        # The env var *name* is inlined as a literal (not passed as a %s arg)
        # so this log statement carries no reference to `_ENV_PASSPHRASE` —
        # CodeQL's clear-text-logging heuristic flags any variable named
        # like a secret regardless of what it actually holds (here, a
        # constant env-var name, never the passphrase value itself, which
        # lives only in the local `passphrase` var in _passphrase_master_key
        # and is never logged). Keep this literal in sync with
        # `_ENV_PASSPHRASE` above if that constant ever changes.
        logger.warning(
            "OS keyring unavailable and VELUNE_MASTER_PASSPHRASE not set — "
            "credentials are encrypted with a machine-derived key that offers "
            "only weak at-rest protection. Set VELUNE_MASTER_PASSPHRASE to a "
            "strong secret for real encryption on this host."
        )
        _warned_no_protection = True
    return _get_fallback_key()


def _candidate_master_keys() -> Iterator[bytes]:
    """Yield keys to try when decrypting, most-likely first, de-duplicated.

    Ordering keyring -> passphrase -> machine fallback lets a store survive a
    transient keyring failure or a host that gained a passphrase after a
    machine-key-encrypted write, instead of failing closed. Each candidate is
    computed lazily, one at a time: the caller's loop tries a candidate and
    stops as soon as one decrypts successfully, so the (deliberately slow,
    390k-iteration) passphrase KDF and the machine-fallback hash are only ever
    paid for when the keyring candidate actually fails — not on every decrypt.
    """
    seen: list[bytes] = []

    def _emit(candidate: bytes | None) -> Iterator[bytes]:
        if candidate is not None and candidate not in seen:
            seen.append(candidate)
            yield candidate

    yield from _emit(get_or_create_master_key())  # primary (keyring -> passphrase -> fallback)
    yield from _emit(_passphrase_master_key())  # store written before a keyring became available
    yield from _emit(_get_fallback_key())  # legacy machine-key store predating a passphrase


def encrypt_credentials(plaintext: str) -> bytes:
    """Encrypt credentials JSON string to bytes using AES-GCM."""
    key = get_or_create_master_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # The ciphertext is stored with the 12-byte nonce prepended
    return nonce + ciphertext


def decrypt_credentials(data: bytes) -> str:
    """Decrypt credentials bytes to JSON string."""
    if len(data) < 12:
        raise ValueError("Encrypted data is too short to contain a nonce.")

    nonce = data[:12]
    ciphertext = data[12:]

    # Try every candidate key rather than a single one so a store written under
    # the machine fallback still opens after a passphrase is configured (and
    # vice versa), and a transient keyring hiccup does not brick decryption.
    last_exc: InvalidTag | None = None
    for key in _candidate_master_keys():
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
        except InvalidTag as exc:
            last_exc = exc
            continue

    # AES-GCM raises InvalidTag with an empty message, which surfaces downstream
    # as a blank "Failed to load credentials" log. Translate it into an
    # actionable error: the key genuinely changed (new machine, reset OS
    # keyring, changed passphrase) or the file is corrupt.
    raise DecryptionError(
        "Stored credentials could not be decrypted — the encryption key changed "
        "(new machine, reset OS keyring, or changed VELUNE_MASTER_PASSPHRASE) or "
        "the file is corrupt. Re-add your keys with `velune setup`."
    ) from last_exc


def _derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from *passphrase* using PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_with_passphrase(plaintext: str, passphrase: str) -> bytes:
    """Encrypt *plaintext* with a key derived from *passphrase* (AES-GCM).

    Unlike :func:`encrypt_credentials`, the key here is derived solely from the
    passphrase — not the OS keyring or machine identity — so the result can be
    decrypted on a different machine given the same passphrase. Used for
    portable exports (e.g. backup archives) that must never carry secrets in
    the clear but still need to survive a move to a new install.
    """
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key_from_passphrase(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return salt + nonce + ciphertext


def decrypt_with_passphrase(data: bytes, passphrase: str) -> str:
    """Decrypt data produced by :func:`encrypt_with_passphrase`.

    Raises :class:`DecryptionError` on a wrong passphrase or corrupt data.
    """
    if len(data) < _SALT_LEN + _NONCE_LEN:
        raise DecryptionError("Encrypted data is too short to contain a salt and nonce.")

    salt = data[:_SALT_LEN]
    nonce = data[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
    ciphertext = data[_SALT_LEN + _NONCE_LEN :]
    key = _derive_key_from_passphrase(passphrase, salt)

    try:
        plaintext_bytes = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptionError("Wrong passphrase or corrupted archive.") from exc
    return plaintext_bytes.decode("utf-8")
