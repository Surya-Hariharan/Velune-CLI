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
