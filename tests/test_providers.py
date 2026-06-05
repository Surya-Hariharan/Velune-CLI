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
