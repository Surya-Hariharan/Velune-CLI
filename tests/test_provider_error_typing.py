"""OpenAI, Anthropic and Gemini translate HTTP failures into the typed errors the
retry layer and the CLI rely on (bad key / rate limit / retired model / bad request)."""

from __future__ import annotations

import httpx
import pytest

from velune.core.errors.provider import (
    InferenceError,
    InvalidRequestError,
    ModelNotFoundError,
    ProviderAuthenticationError,
    RateLimitError,
)
from velune.core.types.inference import InferenceRequest
from velune.providers.adapters.anthropic import AnthropicProvider
from velune.providers.adapters.google import GoogleProvider
from velune.providers.adapters.groq import GroqProvider
from velune.providers.adapters.openai import OpenAIProvider


def _provider(kind: str, status: int, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"}, headers=headers or {})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    provider = {
        "openai": lambda: OpenAIProvider(api_key="k"),
        "anthropic": lambda: AnthropicProvider(api_key="k"),
        "google": lambda: GoogleProvider(api_key="k"),
    }[kind]()
    provider.client = client
    return provider


def _req(model: str = "m") -> InferenceRequest:
    return InferenceRequest(model_id=model, messages=[{"role": "user", "content": "hi"}])


async def _drain(provider):
    return [chunk async for chunk in provider.stream(_req())]


CASES = [
    (401, ProviderAuthenticationError),
    (403, ProviderAuthenticationError),
    (404, ModelNotFoundError),
    (400, InvalidRequestError),
    (429, RateLimitError),
    (500, InferenceError),
]


@pytest.mark.parametrize("kind", ["openai", "anthropic", "google"])
@pytest.mark.parametrize(("status", "expected"), CASES)
async def test_infer_maps_status_to_typed_error(kind, status, expected):
    with pytest.raises(expected) as info:
        await _provider(kind, status).infer(_req())
    # Deterministic verdicts must be their own type, not the retryable generic error.
    assert type(info.value) is expected or expected is InferenceError


@pytest.mark.parametrize("kind", ["openai", "anthropic", "google"])
@pytest.mark.parametrize(("status", "expected"), CASES)
async def test_stream_maps_status_to_typed_error(kind, status, expected):
    with pytest.raises(expected):
        await _drain(_provider(kind, status))


@pytest.mark.parametrize("kind", ["openai", "anthropic", "google"])
async def test_rate_limit_carries_retry_after(kind):
    with pytest.raises(RateLimitError) as info:
        await _provider(kind, 429, {"retry-after": "7"}).infer(_req())
    assert info.value.retry_after == 7.0


def test_openai_reasoning_models_use_max_completion_tokens():
    req = InferenceRequest(
        model_id="o3-mini",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
        stop_sequences=["END"],
    )
    payload = OpenAIProvider(api_key="k")._chat_payload(req)
    assert payload["max_completion_tokens"] == 100
    for banned in ("max_tokens", "temperature", "top_p", "stop"):
        assert banned not in payload


def test_openai_classic_models_and_compatible_providers_keep_max_tokens():
    req = InferenceRequest(
        model_id="gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=50
    )
    payload = OpenAIProvider(api_key="k")._chat_payload(req)
    assert payload["max_tokens"] == 50 and "max_completion_tokens" not in payload
    assert "temperature" in payload

    # A reasoning-looking id on an OpenAI-compatible provider is left untouched.
    groq_req = InferenceRequest(
        model_id="o3-mini", messages=[{"role": "user", "content": "hi"}], max_tokens=50
    )
    groq_payload = GroqProvider(api_key="k")._chat_payload(groq_req)
    assert groq_payload["max_tokens"] == 50 and "max_completion_tokens" not in groq_payload
