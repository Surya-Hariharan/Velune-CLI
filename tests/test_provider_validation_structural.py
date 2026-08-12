"""Structural (pre-network) credential validation in providers/validation.py.

Regression coverage for a gap where ``ValidationStatus.MALFORMED_KEY`` existed
in the enum and was already wired into ``verifier.py``'s "this is a statement
about the key" set, but no validator ever actually produced it — every
obviously-wrong paste (empty, whitespace, absurdly short, wrong prefix) still
paid for a live network round-trip and came back as a generic error instead of
MALFORMED_KEY. This never claims a key IS valid — only that a network call is
not worth making for one that is obviously not.
"""

from __future__ import annotations

import pytest

from velune.providers.validation import ValidationStatus, validate_provider


@pytest.mark.asyncio
async def test_empty_key_is_malformed_without_a_network_call():
    result = await validate_provider("anthropic", "")
    assert result.status == ValidationStatus.MALFORMED_KEY
    assert result.ok is False


@pytest.mark.asyncio
async def test_whitespace_only_key_is_malformed():
    result = await validate_provider("openai", "   \n\t  ")
    assert result.status == ValidationStatus.MALFORMED_KEY


@pytest.mark.asyncio
async def test_key_with_embedded_whitespace_is_malformed():
    result = await validate_provider("openai", "sk-abc def ghi jklmno")
    assert result.status == ValidationStatus.MALFORMED_KEY


@pytest.mark.asyncio
async def test_absurdly_short_key_is_malformed():
    result = await validate_provider("groq", "gsk_x")
    assert result.status == ValidationStatus.MALFORMED_KEY


@pytest.mark.asyncio
async def test_wrong_prefix_for_a_known_provider_is_malformed():
    # A real-shaped OpenAI key handed to Anthropic — right length, wrong house.
    result = await validate_provider("anthropic", "sk-not-an-anthropic-key-1234567890")
    assert result.status == ValidationStatus.MALFORMED_KEY


@pytest.mark.asyncio
async def test_correct_prefix_and_length_passes_structural_check(monkeypatch):
    """A plausibly-shaped key must reach the real network validator — this is
    NOT where "valid" is decided, only where "obviously wrong" is filtered."""
    import velune.providers.validation as validation

    called = {}

    async def _fake_network_call(api_key: str):
        called["key"] = api_key
        return validation.ValidationResult(
            provider_id="anthropic", status=ValidationStatus.OK, message="ok"
        )

    monkeypatch.setitem(validation._VALIDATORS, "anthropic", _fake_network_call)

    result = await validate_provider("anthropic", "sk-ant-plausible-shaped-key-1234567890")
    assert result.status == ValidationStatus.OK
    assert called["key"] == "sk-ant-plausible-shaped-key-1234567890"


@pytest.mark.asyncio
async def test_structural_check_is_skipped_for_keyless_local_providers(monkeypatch):
    """Ollama/LM Studio take no key at all; an empty string must reach their
    real (keyless) validator rather than being rejected as malformed."""
    import velune.providers.validation as validation

    called = []

    async def _fake_ollama(api_key: str = ""):
        called.append(api_key)
        return validation.ValidationResult(
            provider_id="ollama", status=ValidationStatus.OK, message="reachable"
        )

    monkeypatch.setitem(validation._VALIDATORS, "ollama", _fake_ollama)

    result = await validate_provider("ollama", "")
    assert result.status == ValidationStatus.OK
    assert called == [""]


@pytest.mark.asyncio
async def test_a_malformed_key_is_never_reported_as_ok():
    result = await validate_provider("anthropic", "short")
    assert result.ok is False
    assert result.status == ValidationStatus.MALFORMED_KEY
