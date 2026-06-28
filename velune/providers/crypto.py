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

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("velune.providers.crypto")

_SERVICE = "velune"
_USERNAME = "master_key"


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
