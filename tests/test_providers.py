"""Tests for provider registry, discovery, and mock inference."""
import pytest

from velune.core.types.inference import InferenceRequest, InferenceResponse
from velune.providers.discovery.anthropic import AnthropicDiscovery
from velune.providers.registry import ProviderRegistry


def test_provider_registry_lists_defaults():
    registry = ProviderRegistry()
    providers = registry.list_providers()
    for expected in ("ollama", "openai", "anthropic", "lmstudio"):
        assert expected in providers, f"Expected '{expected}' in provider list, got {providers}"


@pytest.mark.asyncio
async def test_anthropic_model_ids_are_current(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key")
    discovery = AnthropicDiscovery()
    models = await discovery.discover()

    assert len(models) > 0, "Expected at least one model from AnthropicDiscovery"
    for model in models:
        assert "20240229" not in model.model_id, f"Stale model ID: {model.model_id}"
        assert "20240307" not in model.model_id, f"Stale model ID: {model.model_id}"


def test_groq_models_are_free():
    from velune.providers.adapters.groq import GROQ_MODELS
    assert len(GROQ_MODELS) > 0
    for m in GROQ_MODELS:
        assert m.free_tier is True, f"{m.model_id} missing free_tier=True"
        assert m.cost_per_1k_tokens == 0.0, f"{m.model_id} has non-zero cost"
        assert m.provider_id == "groq", f"{m.model_id} has wrong provider_id"


@pytest.mark.asyncio
async def test_groq_discovery_skips_without_key(monkeypatch):
    monkeypatch.setattr("velune.providers.keystore.has_key", lambda x: False)
    from velune.providers.discovery.groq import GroqDiscovery
    result = await GroqDiscovery().discover()
    assert result == []


@pytest.mark.asyncio
async def test_mock_provider_infer(mock_provider):
    request = InferenceRequest(
        model_id="test-model",
        messages=[{"role": "user", "content": "Hello"}],
    )
    response = await mock_provider.infer(request)

    assert isinstance(response, InferenceResponse)
    assert len(response.content) > 0
    assert response.finish_reason == "stop"
    assert mock_provider.call_count == 1
    assert mock_provider.last_request is request
