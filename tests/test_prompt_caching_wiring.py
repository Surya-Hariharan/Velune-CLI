"""Anthropic prompt caching wired into ordinary chat, not just the Council.

ContextCacheManager.prepare()/record() previously ran only from
BaseCouncilAgent.deliberate() (velune/cognition/council/base.py) — a normal
REPL chat turn, whether it went through ToolLoopRunner (the common case for
any tool-calling model, which every Claude model is) or the StreamRenderer
fallback path, never annotated its request with cache_control blocks at all.
These tests pin that both paths now call through the same
ContextCacheManager used by the Council.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from velune.context.cache.manager import reset_all_managers
from velune.context.cache.providers import ANTHROPIC_CACHE_PAYLOAD_KEY
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.orchestration.tool_loop import ToolLoopRunner


@pytest.fixture(autouse=True)
def _reset_cache_singletons():
    reset_all_managers()
    yield
    reset_all_managers()


def _request() -> InferenceRequest:
    return InferenceRequest(
        model_id="claude-sonnet-4-5",
        messages=[
            {"role": "system", "content": "You are a careful coding assistant."},
            {"role": "user", "content": "Fix the auth bug in login.py"},
        ],
    )


class _FakeAnthropicProvider:
    """Records every request handed to infer()/stream() for inspection."""

    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(self) -> None:
        self.provider_id = "anthropic"
        self.seen_requests: list[InferenceRequest] = []

    def get_capabilities(self):
        return SimpleNamespace(supports_streaming=False, supports_function_calling=True)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.seen_requests.append(request)
        return InferenceResponse(
            content="done",
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=10,
            latency_ms=1.0,
            metadata={"raw_usage": {"cache_read_input_tokens": 123}},
        )

    async def stream(self, request: InferenceRequest):
        self.seen_requests.append(request)
        yield StreamChunk(content="done", finish_reason="stop")


def _cache_payload(request: InferenceRequest) -> dict:
    return request.metadata.get(ANTHROPIC_CACHE_PAYLOAD_KEY) or {}


async def test_tool_loop_runner_annotates_requests_with_anthropic_cache_blocks():
    provider = _FakeAnthropicProvider()
    runner = ToolLoopRunner(provider, registry=None)

    result = await runner.run(_request())

    assert result.content == "done"
    assert provider.seen_requests, "provider was never called"
    payload = _cache_payload(provider.seen_requests[0])
    assert payload, "request reaching the provider carries no cache_control payload"
    # System message is marked ephemeral — it's the stable, repeated-every-turn
    # prefix a tool-calling conversation benefits from caching most.
    system_blocks = payload.get("system")
    assert isinstance(system_blocks, list)
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}


async def test_tool_loop_runner_records_cache_stats_from_response_metadata():
    provider = _FakeAnthropicProvider()
    runner = ToolLoopRunner(provider, registry=None)

    await runner.run(_request())

    metrics = runner._cache_manager.get_metrics()
    assert metrics.cache_read_tokens == 123


async def test_non_anthropic_provider_is_untouched_by_cache_wiring():
    provider = _FakeAnthropicProvider()
    provider.provider_id = "openai"
    runner = ToolLoopRunner(provider, registry=None)

    await runner.run(_request())

    # NoOp provider: request passes through with no cache metadata added.
    assert _cache_payload(provider.seen_requests[0]) == {}


async def test_stream_renderer_fallback_path_annotates_requests(monkeypatch):
    from velune.cli.interrupts import InterruptController
    from velune.cli.stream_renderer import StreamRenderer

    provider = _FakeAnthropicProvider()
    renderer = StreamRenderer(
        console=SimpleNamespace(status=lambda *a, **k: _NullStatus(), print=lambda *a, **k: None),
        interrupts=InterruptController(),
        status_state=SimpleNamespace(last_latency_ms=None, last_tokens_per_sec=None),
    )

    render_result = await renderer.render(provider, _request())

    assert render_result.text == "done"
    assert provider.seen_requests
    assert _cache_payload(provider.seen_requests[0]), "fallback chat path did not apply caching"


class _NullStatus:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
