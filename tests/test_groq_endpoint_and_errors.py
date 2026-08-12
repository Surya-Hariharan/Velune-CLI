"""Groq inference path: endpoint construction, model propagation, and error
classification.

Regression coverage for the production incident where a chat turn against
Groq's `qwen/qwen3-32b` (deprecated by Groq 2026-07-17, see GROQ_MODELS'
comment and test_groq_models.py) surfaced as a raw, retried, "Unexpected
Error". Three separate gaps pinned here:

1. The base_url + "/chat/completions" join must be exactly right (it already
   was — httpx's base_url/relative-path merge is correct here — but this
   guards the contract explicitly, at the transport layer, rather than by
   inference from reading the adapter).
2. A 404 must be classified as ModelNotFoundError, not the generic
   InferenceError bucket every other adapter failure falls into.
3. ModelNotFoundError must be excluded from RetryingProvider's retry set —
   a deterministic 404 was previously retried 3 times before surfacing.

Uses ``httpx.MockTransport`` (part of httpx itself, no extra test
dependency) spliced in under ``httpx.AsyncClient.__init__`` so requests are
intercepted *after* the adapter's real base_url + relative-path join, not
before it — a mock at the ``.post()``/``.stream()`` method level would prove
the adapter called the right method with the right arguments, but not that
httpx actually assembled the URL those two combine into correctly.
"""

from __future__ import annotations

import json

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
from velune.providers.adapters.groq import GroqProvider
from velune.providers.retrying import RETRYABLE_EXCEPTIONS


class _CaptureTransport(httpx.MockTransport):
    """Records every request it handles, answering with *responder*'s result."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []

        def _handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return responder(request)

        super().__init__(_handle)


@pytest.fixture
def wired_transport(monkeypatch):
    """Splice a capturing MockTransport into every httpx.AsyncClient built
    during the test, regardless of which adapter constructs it."""
    holder: dict[str, _CaptureTransport] = {}
    original_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = holder["transport"]
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)

    def _install(responder) -> _CaptureTransport:
        transport = _CaptureTransport(responder)
        holder["transport"] = transport
        return transport

    return _install


def _ok_chat_response(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {},
        },
    )


def test_groq_default_base_url_is_the_documented_openai_compatible_endpoint():
    provider = GroqProvider(api_key="gsk_test")
    assert provider._base_url == "https://api.groq.com/openai/v1"


@pytest.mark.asyncio
async def test_chat_completions_request_hits_exactly_the_documented_url(wired_transport):
    transport = wired_transport(_ok_chat_response)
    provider = GroqProvider(api_key="gsk_test")
    request = InferenceRequest(
        model_id="openai/gpt-oss-120b", messages=[{"role": "user", "content": "hello"}]
    )

    await provider.infer(request)

    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == "https://api.groq.com/openai/v1/chat/completions"
    await provider.shutdown()


@pytest.mark.asyncio
async def test_model_id_reaches_the_request_payload_unmodified(wired_transport):
    """The selected model id must reach Groq exactly as stored — no vendor
    prefix stripping, no id transformation anywhere on the way to the wire."""
    transport = wired_transport(_ok_chat_response)
    provider = GroqProvider(api_key="gsk_test")
    request = InferenceRequest(
        model_id="openai/gpt-oss-120b", messages=[{"role": "user", "content": "hello"}]
    )

    await provider.infer(request)

    sent_payload = json.loads(transport.requests[0].content)
    assert sent_payload["model"] == "openai/gpt-oss-120b"
    await provider.shutdown()


@pytest.mark.asyncio
async def test_streaming_hits_the_same_url_as_non_streaming(wired_transport):
    def _done_stream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: [DONE]\n\n")

    transport = wired_transport(_done_stream)
    provider = GroqProvider(api_key="gsk_test")
    request = InferenceRequest(
        model_id="openai/gpt-oss-120b", messages=[{"role": "user", "content": "hello"}]
    )

    chunks = [c async for c in provider.stream(request)]
    assert chunks == []  # [DONE] immediately, no content — this just proves no exception
    assert str(transport.requests[0].url) == "https://api.groq.com/openai/v1/chat/completions"
    await provider.shutdown()


@pytest.mark.asyncio
async def test_a_404_is_classified_as_model_not_found_not_generic_inference_error(
    wired_transport,
):
    def _not_found(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    wired_transport(_not_found)
    provider = GroqProvider(api_key="gsk_test")
    request = InferenceRequest(
        model_id="qwen/qwen3-32b", messages=[{"role": "user", "content": "hello"}]
    )

    with pytest.raises(ModelNotFoundError):
        await provider.infer(request)
    await provider.shutdown()


@pytest.mark.asyncio
async def test_a_404_during_streaming_is_also_classified_as_model_not_found(wired_transport):
    def _not_found(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    wired_transport(_not_found)
    provider = GroqProvider(api_key="gsk_test")
    request = InferenceRequest(
        model_id="qwen/qwen3-32b", messages=[{"role": "user", "content": "hello"}]
    )

    with pytest.raises(ModelNotFoundError):
        async for _ in provider.stream(request):
            pass
    await provider.shutdown()


@pytest.mark.asyncio
async def test_a_400_is_classified_as_invalid_request_not_generic_inference_error(
    wired_transport,
):
    def _bad_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    wired_transport(_bad_request)
    provider = GroqProvider(api_key="gsk_test")
    request = InferenceRequest(
        model_id="openai/gpt-oss-120b", messages=[{"role": "user", "content": "hello"}]
    )

    with pytest.raises(InvalidRequestError):
        await provider.infer(request)
    await provider.shutdown()


def test_model_not_found_and_invalid_request_are_never_retried():
    assert not issubclass(ModelNotFoundError, RETRYABLE_EXCEPTIONS)
    assert not issubclass(InvalidRequestError, RETRYABLE_EXCEPTIONS)


def test_transient_failures_are_still_retryable():
    """The fix must not accidentally stop retrying real transient failures."""
    assert issubclass(InferenceError, RETRYABLE_EXCEPTIONS)


def test_auth_errors_remain_non_retryable():
    """404/400 reclassification must not disturb the existing 401/403 path:
    retrying a rejected key wastes attempts on something retrying can't fix."""
    assert not issubclass(ProviderAuthenticationError, RETRYABLE_EXCEPTIONS)


def test_rate_limit_errors_remain_retryable_with_their_own_backoff():
    """429 is intentionally still retried — per Retry-After when the provider
    sends one — unlike the deterministic 404/400/401/403 verdicts above."""
    assert issubclass(RateLimitError, RETRYABLE_EXCEPTIONS)
