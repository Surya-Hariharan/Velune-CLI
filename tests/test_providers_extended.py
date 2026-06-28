from unittest.mock import patch

import pytest

from velune.core.types.provider import ProviderHealth
from velune.providers.adapters.anthropic import AnthropicProvider
from velune.providers.adapters.google import GoogleProvider
from velune.providers.adapters.openai import OpenAIProvider


class MockResponse:
    def __init__(self, status_code):
        self.status_code = status_code

@pytest.mark.asyncio
async def test_openai_health_check_success():
    with patch("velune.providers.adapters.openai.get_key", return_value="fake-key"):
        provider = OpenAIProvider()
        with patch("httpx.AsyncClient.get", return_value=MockResponse(200)):
            health = await provider.health_check()
            assert health == ProviderHealth.HEALTHY


@pytest.mark.asyncio
async def test_anthropic_health_check_failure():
    with patch("velune.providers.adapters.anthropic.get_key", return_value="fake-key"):
        provider = AnthropicProvider()
        with patch("httpx.AsyncClient.post", side_effect=Exception("Network Error")):
            health = await provider.health_check()
            assert health == ProviderHealth.UNAVAILABLE


@pytest.mark.asyncio
async def test_google_health_check_unauthenticated():
    with patch("velune.providers.adapters.google.get_key", return_value=None):
        provider = GoogleProvider()
        health = await provider.health_check()
        assert health == ProviderHealth.UNAVAILABLE


@pytest.mark.asyncio
async def test_openai_streaming_mock():
    # Verify that stream capability is declared
    provider = OpenAIProvider()
    caps = provider.get_capabilities()
    assert caps.supports_streaming is True


@pytest.mark.asyncio
async def test_anthropic_tool_calling_mock():
    provider = AnthropicProvider()
    caps = provider.get_capabilities()
    assert caps.supports_function_calling is True
