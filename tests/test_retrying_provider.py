"""RetryingProvider — transparent retry around ModelProvider.infer()/stream().

Previously only the Council orchestrator (velune/cognition/orchestrator.py)
retried a failed inference call; the native tool loop and the REPL's
fallback chat path had no retry at all. Wiring this into
ProviderRegistry.get() gives every call site retry for free.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from velune.core.errors.provider import (
    InferenceError,
    ProviderAuthenticationError,
    RateLimitError,
)
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.providers.retrying import RetryingProvider


def _request() -> InferenceRequest:
    return InferenceRequest(model_id="m", messages=[{"role": "user", "content": "hi"}])


class _FlakyProvider:
    """Fails N times then succeeds; records call count and delays it saw."""

    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(self, fail_times: int, exc_factory) -> None:
        self.provider_id = "fake"
        self._fail_times = fail_times
        self._exc_factory = exc_factory
        self.calls = 0

    def get_capabilities(self):
        return SimpleNamespace(supports_streaming=True, supports_function_calling=True)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc_factory()
        return InferenceResponse(
            content="ok", model_id=request.model_id, finish_reason="stop",
            tokens_used=1, latency_ms=1.0,
        )

    async def stream(self, request: InferenceRequest):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc_factory()
        yield StreamChunk(content="ok", finish_reason="stop")


async def test_infer_retries_transient_failures_and_succeeds():
    inner = _FlakyProvider(2, lambda: InferenceError("boom"))
    provider = RetryingProvider(inner, max_attempts=3)

    result = await provider.infer(_request())

    assert result.content == "ok"
    assert inner.calls == 3


async def test_infer_gives_up_after_max_attempts():
    inner = _FlakyProvider(5, lambda: InferenceError("boom"))
    provider = RetryingProvider(inner, max_attempts=3)

    with pytest.raises(InferenceError):
        await provider.infer(_request())
    assert inner.calls == 3


async def test_infer_never_retries_auth_errors():
    inner = _FlakyProvider(5, lambda: ProviderAuthenticationError("bad key"))
    provider = RetryingProvider(inner, max_attempts=3)

    with pytest.raises(ProviderAuthenticationError):
        await provider.infer(_request())
    assert inner.calls == 1  # no retry wasted on a key rejection


async def test_infer_honors_retry_after_from_rate_limit_error(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("velune.core.retry.asyncio.sleep", fake_sleep)

    inner = _FlakyProvider(1, lambda: RateLimitError("slow down", retry_after=12.5))
    provider = RetryingProvider(inner, max_attempts=3)

    result = await provider.infer(_request())

    assert result.content == "ok"
    assert sleeps == [12.5]


async def test_stream_retries_when_failure_happens_before_first_chunk(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("velune.providers.retrying.asyncio.sleep", fake_sleep)

    inner = _FlakyProvider(1, lambda: InferenceError("boom"))
    provider = RetryingProvider(inner, max_attempts=3)

    chunks = [c async for c in provider.stream(_request())]

    assert [c.content for c in chunks] == ["ok"]
    assert inner.calls == 2


async def test_stream_never_retries_once_a_chunk_already_reached_the_caller():
    class MidStreamFailure:
        provider_id = "fake"
        SUPPORTS_STREAMING_TOOL_CALLS = True

        def __init__(self):
            self.calls = 0

        def get_capabilities(self):
            return SimpleNamespace(supports_streaming=True)

        async def stream(self, request):
            self.calls += 1
            yield StreamChunk(content="partial")
            raise InferenceError("dropped mid-stream")

    inner = MidStreamFailure()
    provider = RetryingProvider(inner, max_attempts=3)

    chunks = []
    with pytest.raises(InferenceError):
        async for chunk in provider.stream(_request()):
            chunks.append(chunk)

    assert [c.content for c in chunks] == ["partial"]
    assert inner.calls == 1  # never restarted — would have duplicated "partial"


def test_supports_streaming_tool_calls_forwarded_from_inner():
    class NoStreamingTools:
        provider_id = "fake"
        SUPPORTS_STREAMING_TOOL_CALLS = False

    assert RetryingProvider(NoStreamingTools()).SUPPORTS_STREAMING_TOOL_CALLS is False

    class WithStreamingTools:
        provider_id = "fake"
        SUPPORTS_STREAMING_TOOL_CALLS = True

    assert RetryingProvider(WithStreamingTools()).SUPPORTS_STREAMING_TOOL_CALLS is True


def test_provider_id_and_unknown_attrs_forwarded():
    class Extra:
        provider_id = "groq"

        def get_provider_info(self):
            return {"is_free_tier": True}

    wrapped = RetryingProvider(Extra())
    assert wrapped.provider_id == "groq"
    assert wrapped.get_provider_info() == {"is_free_tier": True}
