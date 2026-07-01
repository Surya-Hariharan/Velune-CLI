"""Encryption layer for Velune's persistent credentials.

This module provides AES-GCM encryption for the credentials.json file.
It attempts to use the OS keyring (Credential Manager / Keychain) to store
a randomly generated 256-bit master key. If the keyring is unavailable,
it gracefully falls back to a deterministic machine-specific key to ensure
credentials remain encrypted at rest.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger("velune.providers.crypto")

_SERVICE = "velune"
_USERNAME = "master_key"

_PBKDF2_ITERATIONS = 390_000
_SALT_LEN = 16
_NONCE_LEN = 12


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
    """Derive a stable 256-bit fallback key from machine properties."""
    machine_id = _get_machine_id().encode("utf-8")
    salt = b"velune_fallback_salt_v1"

    # We use a simple hash since this is a fallback for when the OS keyring
    # is entirely broken. It prevents plaintext credentials on disk but relies
    # on machine properties for stability.
    hasher = hashlib.sha256()
    hasher.update(machine_id)
    hasher.update(salt)
    return hasher.digest()


def get_or_create_master_key() -> bytes:
    """Retrieve the master key from the OS keyring, or create/store a new one.

    If the keyring is unavailable, falls back to a machine-specific key.
    """
    try:
        import keyring

        key_b64 = keyring.get_password(_SERVICE, _USERNAME)
        if key_b64:
            return base64.b64decode(key_b64.encode("ascii"))
    except Exception as e:
        logger.debug("Failed to read master key from keyring: %s", e)
        return _get_fallback_key()

    # Need to generate and save a new key
    try:
        new_key = AESGCM.generate_key(bit_length=256)
        key_b64 = base64.b64encode(new_key).decode("ascii")
        import keyring

        keyring.set_password(_SERVICE, _USERNAME, key_b64)
        return new_key
    except Exception as e:
        logger.debug("Failed to save master key to keyring: %s", e)
        return _get_fallback_key()


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

    key = get_or_create_master_key()
    aesgcm = AESGCM(key)
    nonce = data[:12]
    ciphertext = data[12:]

    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext_bytes.decode("utf-8")


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
