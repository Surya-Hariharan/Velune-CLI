"""velune/providers/credential_manager.py — the single add-credential entry
point that replaced three independently hand-rolled validate/save sequences
(REPL palette, Typer CLI, onboarding wizard). See 01-provider-management-v2.md
Phase 0.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from velune.providers.credential_manager import (
    add_credential,
    add_credential_sync,
    persist_credential,
)
from velune.providers.keystore import CredentialManager, get_key, has_key, verification_state
from velune.providers.validation import ValidationResult, ValidationStatus


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


def _ok_result(provider_id: str, models: list[str] | None = None) -> ValidationResult:
    return ValidationResult(
        provider_id=provider_id, status=ValidationStatus.OK, message="ok", models=models or []
    )


def _fail_result(provider_id: str, status=ValidationStatus.INVALID_KEY) -> ValidationResult:
    return ValidationResult(provider_id=provider_id, status=status, message="nope")


async def test_add_credential_success_saves_and_marks_verified(mock_config_dir, mock_keyring):
    with patch(
        "velune.providers.credential_manager.validate_provider",
        return_value=_ok_result("anthropic", models=["claude-x"]),
    ):
        result = await add_credential("anthropic", "sk-test", set_as_first_default=False)

    assert result.ok is True
    assert result.saved is True
    assert result.verified is True
    assert has_key("anthropic")
    assert get_key("anthropic") == "sk-test"
    assert str(verification_state("anthropic")) == "verified"


async def test_add_credential_failure_does_not_save_by_default(mock_config_dir, mock_keyring):
    with patch(
        "velune.providers.credential_manager.validate_provider",
        return_value=_fail_result("anthropic"),
    ):
        result = await add_credential("anthropic", "sk-bad", set_as_first_default=False)

    assert result.ok is False
    assert result.saved is False
    assert not has_key("anthropic")


async def test_add_credential_can_save_unverified_on_failure(mock_config_dir, mock_keyring):
    with patch(
        "velune.providers.credential_manager.validate_provider",
        return_value=_fail_result("anthropic", status=ValidationStatus.NETWORK_ERROR),
    ):
        result = await add_credential(
            "anthropic", "sk-offline", save_unverified_on_failure=True, set_as_first_default=False
        )

    assert result.ok is False
    assert result.saved is True
    assert has_key("anthropic")
    assert str(verification_state("anthropic")) == "unverified"


async def test_add_credential_skip_validation(mock_config_dir, mock_keyring):
    result = await add_credential(
        "anthropic", "sk-trusted", skip_validation=True, set_as_first_default=False
    )
    assert result.ok is True
    assert result.verified is False
    assert has_key("anthropic")


async def test_add_credential_unknown_provider(mock_config_dir, mock_keyring):
    result = await add_credential("not-a-real-provider", "x")
    assert result.ok is False
    assert result.unknown_provider is True
    assert result.saved is False


async def test_add_credential_rejects_keyless_provider(mock_config_dir, mock_keyring):
    result = await add_credential("ollama", "irrelevant")
    assert result.ok is False
    assert result.not_a_key_provider is True


async def test_add_credential_sets_first_default(mock_config_dir, mock_keyring):
    with patch(
        "velune.providers.credential_manager.validate_provider",
        return_value=_ok_result("groq"),
    ):
        result = await add_credential("groq", "gsk-test", set_as_first_default=True)

    assert result.became_default is True

    from velune.providers.default_provider import get_default_provider

    assert get_default_provider() == "groq"


def test_add_credential_sync_wraps_the_coroutine(mock_config_dir, mock_keyring):
    with patch(
        "velune.providers.credential_manager.validate_provider",
        return_value=_ok_result("groq"),
    ):
        result = add_credential_sync("groq", "gsk-sync", set_as_first_default=False)
    assert result.ok is True
    assert has_key("groq")


def test_persist_credential_does_not_call_validation(mock_config_dir, mock_keyring):
    """The REPL's "save anyway" branch already knows the verdict — it must
    not re-run a live API call just to persist an unverified key."""
    with patch("velune.providers.credential_manager.validate_provider") as mock_validate:
        became_default = persist_credential("anthropic", "sk-anyway", verified=False)

    mock_validate.assert_not_called()
    assert became_default is False
    assert str(verification_state("anthropic")) == "unverified"
