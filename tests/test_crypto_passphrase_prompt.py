"""First-run passphrase prompt for the credentials master key.

When no OS keyring is available and no VELUNE_MASTER_PASSPHRASE is set,
get_or_create_master_key() used to fall straight through to a weak
machine-derived key. It now gives the user one interactive chance (only on
the very first run, only when stdin is a real terminal) to set a passphrase
that gets persisted for future processes instead.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from velune.providers import crypto


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "user_config_dir", lambda _name: str(tmp_path))
    monkeypatch.delenv("VELUNE_MASTER_PASSPHRASE", raising=False)
    crypto._warned_no_protection = False
    yield tmp_path


def _no_keyring():
    return patch.multiple(
        crypto,
        _keyring_read_key=lambda: None,
        _keyring_create_key=lambda: None,
    )


def test_no_prompt_when_stdin_is_not_a_tty(isolated_config_dir):
    with _no_keyring(), patch("sys.stdin.isatty", return_value=False):
        key = crypto.get_or_create_master_key()
    assert key == crypto._get_fallback_key()
    assert not crypto._passphrase_file_path().exists()


def test_prompted_passphrase_is_used_and_persisted(isolated_config_dir):
    with (
        _no_keyring(),
        patch("sys.stdin.isatty", return_value=True),
        patch("getpass.getpass", return_value="correct horse battery staple"),
    ):
        key = crypto.get_or_create_master_key()

    expected = crypto._derive_key_from_passphrase(
        "correct horse battery staple", crypto._MASTER_PASSPHRASE_SALT
    )
    assert key == expected
    assert crypto._passphrase_file_path().exists()
    assert crypto._stored_passphrase() == "correct horse battery staple"


def test_second_call_reuses_persisted_passphrase_without_reprompting(isolated_config_dir):
    with (
        _no_keyring(),
        patch("sys.stdin.isatty", return_value=True),
        patch("getpass.getpass", return_value="my-passphrase") as mock_getpass,
    ):
        first = crypto.get_or_create_master_key()
        second = crypto.get_or_create_master_key()

    assert first == second
    mock_getpass.assert_called_once()


def test_skipping_the_prompt_falls_back_to_machine_key(isolated_config_dir):
    with (
        _no_keyring(),
        patch("sys.stdin.isatty", return_value=True),
        patch("getpass.getpass", return_value=""),
    ):
        key = crypto.get_or_create_master_key()
    assert key == crypto._get_fallback_key()
    assert not crypto._passphrase_file_path().exists()


def test_prompt_never_fires_once_credentials_file_exists(isolated_config_dir):
    (isolated_config_dir / "credentials.json").write_bytes(b"anything")
    with (
        _no_keyring(),
        patch("sys.stdin.isatty", return_value=True),
        patch("getpass.getpass") as mock_getpass,
    ):
        key = crypto.get_or_create_master_key()
    mock_getpass.assert_not_called()
    assert key == crypto._get_fallback_key()


def test_env_passphrase_takes_priority_over_stored_file(isolated_config_dir, monkeypatch):
    crypto._persist_passphrase("stored-one")
    monkeypatch.setenv("VELUNE_MASTER_PASSPHRASE", "env-one")
    with _no_keyring():
        key = crypto.get_or_create_master_key()
    assert key == crypto._derive_key_from_passphrase("env-one", crypto._MASTER_PASSPHRASE_SALT)


def test_prompt_handles_eof_and_keyboard_interrupt_as_skip(isolated_config_dir):
    with _no_keyring(), patch("sys.stdin.isatty", return_value=True):
        with patch("getpass.getpass", side_effect=EOFError):
            assert crypto._prompt_for_passphrase() is None
        with patch("getpass.getpass", side_effect=KeyboardInterrupt):
            assert crypto._prompt_for_passphrase() is None


# --- Encrypted-at-rest passphrase storage (CodeQL py/clear-text-storage-sensitive-data) ---


SECRET = "test-only-passphrase-do-not-reuse"  # noqa: S105 - synthetic test fixture, not a real credential


def test_persisted_passphrase_file_is_not_plaintext(isolated_config_dir):
    crypto._persist_passphrase(SECRET)
    raw = crypto._passphrase_file_path().read_bytes()
    assert SECRET.encode("utf-8") not in raw
    assert raw.startswith((crypto._PASSPHRASE_MAGIC_DPAPI, crypto._PASSPHRASE_MAGIC_AESGCM))


def test_persisted_passphrase_round_trips(isolated_config_dir):
    crypto._persist_passphrase(SECRET)
    assert crypto._stored_passphrase() == SECRET


def test_aesgcm_fallback_path_is_not_plaintext_and_round_trips(isolated_config_dir, monkeypatch):
    # Simulate DPAPI being unavailable (as it always is on non-Windows) so the
    # AES-GCM, machine-key-derived branch gets coverage on every platform.
    monkeypatch.setattr(crypto, "_dpapi_encrypt", lambda _plaintext: None)
    monkeypatch.setattr(crypto, "_dpapi_decrypt", lambda _blob: None)
    crypto._persist_passphrase(SECRET)
    raw = crypto._passphrase_file_path().read_bytes()
    assert raw.startswith(crypto._PASSPHRASE_MAGIC_AESGCM)
    assert SECRET.encode("utf-8") not in raw
    assert crypto._stored_passphrase() == SECRET


def test_corrupted_stored_passphrase_returns_none_not_a_crash(isolated_config_dir):
    crypto._persist_passphrase(SECRET)
    path = crypto._passphrase_file_path()
    raw = bytearray(path.read_bytes())
    # Flip a byte inside the ciphertext/blob payload (after the magic prefix)
    # so the AEAD tag (or DPAPI blob) no longer validates.
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))
    assert crypto._stored_passphrase() is None


def test_unrecognized_blob_format_returns_none(isolated_config_dir):
    path = crypto._passphrase_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x01\x02not-a-known-format")
    assert crypto._unprotect_passphrase(path.read_bytes()) is None


def test_legacy_plaintext_passphrase_file_is_migrated_and_readable(isolated_config_dir):
    path = crypto._passphrase_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SECRET, encoding="utf-8")  # simulate a pre-migration Velune install

    result = crypto._stored_passphrase()

    assert result == SECRET


def test_legacy_plaintext_file_no_longer_contains_plaintext_after_migration(isolated_config_dir):
    path = crypto._passphrase_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SECRET, encoding="utf-8")

    crypto._stored_passphrase()  # triggers in-place migration

    raw = path.read_bytes()
    assert SECRET.encode("utf-8") not in raw
    assert raw.startswith((crypto._PASSPHRASE_MAGIC_DPAPI, crypto._PASSPHRASE_MAGIC_AESGCM))
    # And the migrated blob is still readable end-to-end.
    assert crypto._stored_passphrase() == SECRET


def test_missing_passphrase_file_returns_none(isolated_config_dir):
    assert not crypto._passphrase_file_path().exists()
    assert crypto._stored_passphrase() is None


def test_empty_passphrase_file_returns_none(isolated_config_dir):
    path = crypto._passphrase_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    assert crypto._stored_passphrase() is None


def test_persist_passphrase_survives_unwritable_directory(isolated_config_dir, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(crypto.Path, "write_bytes", _boom)
    # Must not raise — persistence failures are non-fatal (caller falls back
    # to re-deriving the key from the in-memory passphrase for this process).
    crypto._persist_passphrase(SECRET)


def test_end_to_end_prompted_passphrase_never_touches_disk_as_plaintext(isolated_config_dir):
    with (
        _no_keyring(),
        patch("sys.stdin.isatty", return_value=True),
        patch("getpass.getpass", return_value=SECRET),
    ):
        crypto.get_or_create_master_key()

    raw = crypto._passphrase_file_path().read_bytes()
    assert SECRET.encode("utf-8") not in raw
