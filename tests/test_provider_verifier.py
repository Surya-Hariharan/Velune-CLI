"""providers/verifier.py — re-verification must not conflate "the key is bad"
with "the network/provider had a bad moment".

Regression coverage for Part 14/15 of the /connect hardening: a transient
network failure or rate limit must leave a previously-valid credential's
stored state untouched (so it's retried later), while a real rejection
(invalid/expired/revoked/malformed/permission-denied) must flip it to
INVALID immediately.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from velune.providers.crypto import DecryptionError  # noqa: F401 - sanity import
from velune.providers.keystore import CredentialManager, save_key, verification_state
from velune.providers.validation import ValidationResult, ValidationStatus
from velune.providers.verifier import reverify


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


def _result(status: ValidationStatus) -> ValidationResult:
    return ValidationResult(provider_id="anthropic", status=status, message="x")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        ValidationStatus.INVALID_KEY,
        ValidationStatus.EXPIRED_KEY,
        ValidationStatus.REVOKED_KEY,
        ValidationStatus.MALFORMED_KEY,
        ValidationStatus.PERMISSION_DENIED,
    ],
)
async def test_a_real_rejection_marks_the_credential_invalid(mock_config_dir, mock_keyring, status):
    save_key("anthropic", "sk-ant-was-good", verified=True)
    assert str(verification_state("anthropic")) == "verified"

    with patch("velune.providers.verifier.validate_provider", return_value=_result(status)):
        await reverify("anthropic")

    assert str(verification_state("anthropic")) == "invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        ValidationStatus.NETWORK_ERROR,
        ValidationStatus.RATE_LIMITED,
        ValidationStatus.UNKNOWN_ERROR,
    ],
)
async def test_a_transient_failure_leaves_the_credential_state_untouched(
    mock_config_dir, mock_keyring, status
):
    """A previously-valid key must not be marked invalid just because this one
    probe couldn't reach the provider or got rate-limited."""
    save_key("anthropic", "sk-ant-was-good", verified=True)

    with patch("velune.providers.verifier.validate_provider", return_value=_result(status)):
        await reverify("anthropic")

    # Still "verified" from the earlier save — not flipped to invalid, and
    # the stale TTL clock is untouched (state didn't change to unverified either).
    assert str(verification_state("anthropic")) != "invalid"


@pytest.mark.asyncio
async def test_a_successful_reverify_marks_the_credential_verified_again(
    mock_config_dir, mock_keyring
):
    save_key("anthropic", "sk-ant-key", verified=False)
    assert str(verification_state("anthropic")) == "unverified"

    ok = ValidationResult(
        provider_id="anthropic", status=ValidationStatus.OK, message="ok", models=["m1", "m2"]
    )
    with patch("velune.providers.verifier.validate_provider", return_value=ok):
        await reverify("anthropic")

    assert str(verification_state("anthropic")) == "verified"


@pytest.mark.asyncio
async def test_reverify_reads_the_currently_stored_key(mock_config_dir, mock_keyring):
    save_key("anthropic", "sk-ant-current", verified=False)
    seen = {}

    async def _capture(pid, key):
        seen["key"] = key
        return _result(ValidationStatus.OK)

    with patch("velune.providers.verifier.validate_provider", _capture):
        await reverify("anthropic")

    assert seen["key"] == "sk-ant-current"
