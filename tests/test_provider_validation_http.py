"""Validation tells "bad key" apart from every other failure, for real HTTP shapes.

All traffic goes to an in-process mock transport; no provider is contacted.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import httpx
import pytest

from tests.http_fakes import install_mock_transport, isolate_keystore, refuse_all, time_out
from velune.providers import endpoints
from velune.providers import keystore as ks
from velune.providers.credential_manager import add_credential
from velune.providers.validation import ValidationStatus, validate_provider

OPENAI_KEY = "sk-test-0000000000000000000000000"
ANTHROPIC_KEY = "sk-ant-test-000000000000000000000"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path)
    endpoints.configure_base_url("openai", None)
    endpoints.configure_base_url("anthropic", None)
    with patch(
        "velune.providers.crypto.get_or_create_master_key",
        return_value=base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="),
    ):
        yield
    endpoints.configure_base_url("openai", None)
    endpoints.configure_base_url("anthropic", None)


def _respond(status: int, body=None, text: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body if body is not None else {})

    return handler


@pytest.mark.parametrize("pid,key", [("openai", OPENAI_KEY), ("anthropic", ANTHROPIC_KEY)])
@pytest.mark.parametrize(
    ("status", "body", "expected", "rejects"),
    [
        (401, {"error": {"message": "Incorrect API key"}}, ValidationStatus.INVALID_KEY, True),
        (401, {"error": {"message": "Your key has expired"}}, ValidationStatus.EXPIRED_KEY, True),
        (403, {"error": {"message": "model access"}}, ValidationStatus.PERMISSION_DENIED, False),
        (404, {}, ValidationStatus.UNKNOWN_ERROR, False),
        (408, {}, ValidationStatus.TIMEOUT, False),
        (422, {"error": "bad"}, ValidationStatus.UNKNOWN_ERROR, False),
        (429, {}, ValidationStatus.RATE_LIMITED, False),
        (500, {}, ValidationStatus.PROVIDER_UNAVAILABLE, False),
        (503, {}, ValidationStatus.PROVIDER_UNAVAILABLE, False),
    ],
)
async def test_http_status_maps_to_the_right_category(
    monkeypatch, pid, key, status, body, expected, rejects
):
    install_mock_transport(monkeypatch, _respond(status, body))
    result = await validate_provider(pid, key)
    assert result.status is expected
    assert result.rejects_key is rejects
    assert key not in result.message


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (time_out, ValidationStatus.TIMEOUT),
        (refuse_all, ValidationStatus.NETWORK_ERROR),
        (_respond(200, text="<html>gateway</html>"), ValidationStatus.UNKNOWN_ERROR),
    ],
)
async def test_transport_failures_are_inconclusive(monkeypatch, handler, expected):
    install_mock_transport(monkeypatch, handler)
    result = await validate_provider("openai", OPENAI_KEY)
    assert result.status is expected
    assert result.inconclusive


async def test_success_returns_every_listed_model(monkeypatch):
    data = {"data": [{"id": f"gpt-{i}"} for i in range(50)]}
    install_mock_transport(monkeypatch, _respond(200, data))
    result = await validate_provider("openai", OPENAI_KEY)
    assert result.ok and len(result.models) == 50


async def test_configured_base_url_is_used_for_validation(monkeypatch):
    seen = install_mock_transport(monkeypatch, _respond(200, {"data": []}))
    endpoints.configure_base_url("openai", "https://gateway.example/v1/")
    endpoints.configure_base_url("anthropic", "https://claude-proxy.example/v1")

    await validate_provider("openai", OPENAI_KEY)
    await validate_provider("anthropic", ANTHROPIC_KEY)

    assert str(seen[0].url).startswith("https://gateway.example/v1/models")
    # A copied ".../v1" base must not produce /v1/v1/models.
    assert seen[1].url.path == "/v1/models"
    assert seen[1].url.host == "claude-proxy.example"


# ---------------------------------------------------------------------------
# add_credential: what gets saved, and how it is reported
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_inconclusive_check_saves_the_key_unverified(monkeypatch, status):
    install_mock_transport(monkeypatch, _respond(status))
    result = await add_credential("openai", OPENAI_KEY, set_as_first_default=False)
    assert result.saved and not result.ok and not result.verified
    assert result.summary().startswith("Credential saved, but validation could not be completed")
    assert ks.get_key("openai") == OPENAI_KEY
    assert ks.verification_state("openai") is ks.KeyState.UNVERIFIED


async def test_offline_check_saves_the_key(monkeypatch):
    install_mock_transport(monkeypatch, refuse_all)
    result = await add_credential("openai", OPENAI_KEY, set_as_first_default=False)
    assert result.saved and ks.get_key("openai") == OPENAI_KEY


async def test_rejected_key_is_not_saved(monkeypatch):
    install_mock_transport(monkeypatch, _respond(401, {"error": {"message": "bad key"}}))
    result = await add_credential("openai", OPENAI_KEY, set_as_first_default=False)
    assert not result.saved
    assert ks.get_key("openai") is None
    assert "Authentication failed" in result.summary()


async def test_store_failure_is_reported_never_as_success(monkeypatch):
    from velune.providers import credential_manager

    install_mock_transport(monkeypatch, _respond(200, {"data": []}))

    def boom(*_a, **_k):
        raise ks.CredentialStoreError("disk full")

    monkeypatch.setattr(credential_manager, "save_key", boom)
    result = await add_credential("openai", OPENAI_KEY, set_as_first_default=False)
    assert not result.ok and not result.saved
    assert result.store_error and "Could not save the credential" in result.summary()


async def test_pasted_key_whitespace_is_trimmed_before_saving(monkeypatch):
    install_mock_transport(monkeypatch, _respond(200, {"data": []}))
    await add_credential("openai", f"  {OPENAI_KEY}\n", set_as_first_default=False)
    assert ks.get_key("openai") == OPENAI_KEY


# ---------------------------------------------------------------------------
# 403 never invalidates a credential
# ---------------------------------------------------------------------------


def test_a_403_during_inference_does_not_mark_the_key_invalid(monkeypatch):
    from velune.cognition.council.base import BaseCouncilAgent
    from velune.core.errors.provider import ProviderAuthenticationError, ProviderPermissionError

    ks.save_key("openai", OPENAI_KEY, verified=True)
    BaseCouncilAgent._note_provider_failure("openai", ProviderPermissionError("no access to model"))
    assert ks.verification_state("openai") is ks.KeyState.VERIFIED

    BaseCouncilAgent._note_provider_failure("openai", ProviderAuthenticationError("401"))
    assert ks.verification_state("openai") is ks.KeyState.INVALID


async def test_invalid_keys_are_rechecked_after_the_interval(monkeypatch):
    import datetime as dt

    from velune.providers import verifier
    from velune.providers.validation import ValidationResult

    ks.save_key("openai", OPENAI_KEY, verified=True)
    ks.mark_invalid("openai", reason="old 403 verdict")
    assert ks.list_recheck_due_providers() == []  # just checked

    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ks.INVALID_RECHECK_SECONDS + 5)
    ks._manager.update_status("openai", "invalid", last_checked=stamp.isoformat())
    assert ks.list_recheck_due_providers() == ["openai"]

    async def accept(pid, key=""):
        return ValidationResult(pid, ValidationStatus.OK, "ok")

    monkeypatch.setattr(verifier, "validate_provider", accept)
    await verifier.reverify_stale()
    assert ks.verification_state("openai") is ks.KeyState.VERIFIED
