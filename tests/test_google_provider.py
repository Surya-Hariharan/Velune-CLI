"""Tests for the Google Gemini provider adapter."""

from unittest.mock import patch

import pytest

from velune.core.types.inference import InferenceRequest
from velune.core.types.provider import ProviderHealth
from velune.providers.adapters.google import _MODELS, GoogleProvider


def test_gemini_models_not_empty():
    assert len(_MODELS) >= 4


def test_gemini_flash_is_free():
    flash = next(m for m in _MODELS if m.model_id == "gemini-2.0-flash")
    assert flash.cost_per_1k_tokens == 0.000075
    assert "free" in flash.tags


def test_gemini_15_pro_has_huge_context():
    pro = next(m for m in _MODELS if m.model_id == "gemini-1.5-pro")
    assert pro.context_length >= 1_000_000


def test_gemini_thinking_model_present():
    thinking = next((m for m in _MODELS if m.model_id == "gemini-2.0-flash-thinking-exp"), None)
    assert thinking is not None
    assert thinking.cost_per_1k_tokens == 0.0


def test_message_conversion_separates_system():
    with patch("velune.providers.adapters.google.get_key", return_value="fake-key"):
        provider = GoogleProvider()

    request = InferenceRequest(
        model_id="gemini-2.0-flash",
        messages=[
            {"role": "system", "content": "You are a coding expert."},
            {"role": "user", "content": "Hello"},
        ],
        temperature=0.3,
        max_tokens=100,
    )
    payload = provider._convert_messages(request)
    assert "systemInstruction" in payload
    assert len(payload["contents"]) == 1
    assert payload["contents"][0]["role"] == "user"


def test_message_conversion_assistant_becomes_model():
    with patch("velune.providers.adapters.google.get_key", return_value="fake-key"):
        provider = GoogleProvider()

    request = InferenceRequest(
        model_id="gemini-2.0-flash",
        messages=[
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ],
        temperature=0.3,
        max_tokens=100,
    )
    payload = provider._convert_messages(request)
    roles = [c["role"] for c in payload["contents"]]
    assert "model" in roles
    assert "assistant" not in roles


def test_message_conversion_no_system_skips_instruction():
    with patch("velune.providers.adapters.google.get_key", return_value="fake-key"):
        provider = GoogleProvider()

    request = InferenceRequest(
        model_id="gemini-2.0-flash",
        messages=[{"role": "user", "content": "Hi"}],
        temperature=0.7,
    )
    payload = provider._convert_messages(request)
    assert "systemInstruction" not in payload


@pytest.mark.asyncio
async def test_health_check_unhealthy_without_key():
    with patch("velune.providers.adapters.google.get_key", return_value=None):
        provider = GoogleProvider()
    result = await provider.health_check()
    assert result == ProviderHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_discovery_skips_without_key(monkeypatch):
    import velune.providers.discovery.google as gdiscovery

    monkeypatch.setattr(gdiscovery, "get_key", lambda x: None)
    from velune.providers.discovery.google import GoogleDiscovery

    result = await GoogleDiscovery().discover()
    assert result == []


@pytest.mark.asyncio
async def test_discovery_returns_models_with_key(monkeypatch):
    import velune.providers.discovery.google as gdiscovery

    monkeypatch.setattr(gdiscovery, "get_key", lambda x: "fake-key")
    from velune.providers.discovery.google import GoogleDiscovery

    result = await GoogleDiscovery().discover()
    assert len(result) >= 4
    model_ids = [m.model_id for m in result]
    assert "gemini-2.0-flash" in model_ids
    assert "gemini-2.0-flash-thinking-exp" in model_ids
