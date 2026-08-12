"""Credentials must never appear, even partially, in diagnostic output.

Regression coverage: `/doctor` and `velune provider inspect` both used to
slice and print a fragment of the real API key (first 4-6 + last 4
characters) as a "configured" indicator. That output can end up in a pasted
bug report, a shared terminal recording, or CI logs — and even a partial key
materially narrows a brute-force search. Both surfaces must report
configured/verified *state* only, never any character of the key itself.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from velune.providers.keystore import CredentialManager, save_key


@pytest.fixture
def mock_config_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("velune.providers.keystore.user_config_dir", return_value=str(tmp_path / "cfg")):
        CredentialManager._instance = None
        from velune.providers.keystore import _manager

        _manager._init()
        yield tmp_path
        CredentialManager._instance = None
        _manager._init()


@pytest.fixture
def mock_keyring():
    with patch("velune.providers.crypto.get_or_create_master_key") as mock_get_key:
        mock_get_key.return_value = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
        yield mock_get_key


_SECRET = "sk-ant-super-secret-value-do-not-leak-1234567890"


def test_doctor_openai_check_never_includes_key_characters(mock_config_dir, mock_keyring):
    save_key("openai", "sk-" + "leak-me-not-1234567890", verified=True)

    from velune.cli.commands.doctor import _check_openai_api_key

    result = _check_openai_api_key()
    assert "leak-me-not" not in result["message"]
    assert result["status"] == "ok"


def test_doctor_anthropic_check_never_includes_key_characters(mock_config_dir, mock_keyring):
    save_key("anthropic", _SECRET, verified=True)

    from velune.cli.commands.doctor import _check_anthropic_api_key

    result = _check_anthropic_api_key()
    assert _SECRET not in result["message"]
    assert _SECRET[:6] not in result["message"]
    assert _SECRET[-4:] not in result["message"]
    assert result["status"] == "ok"


def test_provider_inspect_never_includes_key_characters(mock_config_dir, mock_keyring, monkeypatch):
    save_key("anthropic", _SECRET, verified=True)

    from velune.cli.commands import providers as providers_cmd

    printed: list[str] = []
    monkeypatch.setattr(providers_cmd.console, "print", lambda *a, **k: printed.append(str(a)))

    providers_cmd.inspect_provider(provider_id="anthropic", no_live=True)

    joined = "\n".join(printed)
    assert _SECRET not in joined
    assert _SECRET[:6] not in joined
    assert _SECRET[-4:] not in joined
