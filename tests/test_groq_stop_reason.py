"""Phase 4E-1.5: Groq keeps its native stop reason in ``InferenceResponse.metadata["stop_reason"]``.

Every test uses ``httpx.MockTransport``; nothing contacts Groq. The key below is a fake placeholder.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest

from velune.core.types.inference import InferenceRequest
from velune.providers.adapters.groq import GroqProvider
from velune.providers.adapters.openai import OpenAIProvider

MODEL = "llama-3.3-70b-versatile"
FAKE_KEY = "gsk_test_placeholder"


def tool_call(args: Any = '{"path": "a.txt"}', name: str = "read_file", call_id: str = "call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def body(content=None, calls=None, **choice: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": message, **choice}], "usage": {"total_tokens": 3}}


@pytest.fixture
def groq(monkeypatch):
    """A GroqProvider whose HTTP client answers from ``reply[0]`` and never leaves the process."""
    reply: list[Any] = [None]
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=reply[0])

    original = httpx.AsyncClient.__init__

    def patched(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handle)
        return original(self, *a, **kw)

    def no_network(*a, **kw):
        raise AssertionError("a real connection was attempted")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    monkeypatch.setattr(socket, "create_connection", no_network)
    provider = GroqProvider(api_key=FAKE_KEY)

    def make(response: Any) -> GroqProvider:
        reply[0] = response
        return provider

    make.sent = sent  # type: ignore[attr-defined]
    return make


async def infer(provider: GroqProvider):
    request = InferenceRequest(model_id=MODEL, messages=[{"role": "user", "content": "hi"}])
    try:
        return await provider.infer(request)
    finally:
        await provider.shutdown()


# ── what is preserved ─────────────────────────────────────────────────────────


async def test_a_complete_tool_call_keeps_the_native_tool_calls_reason(groq):
    out = await infer(groq(body(calls=[tool_call()], finish_reason="tool_calls")))
    assert out.metadata["stop_reason"] == "tool_calls"
    assert out.finish_reason == "tool_calls" and out.tool_calls is not None
    assert out.tool_calls[0].arguments == {"path": "a.txt"}


async def test_a_text_answer_keeps_its_native_reason(groq):
    out = await infer(groq(body("hello", finish_reason="stop")))
    assert out.metadata["stop_reason"] == "stop"
    assert out.finish_reason == "stop" and out.content == "hello" and out.tool_calls is None


async def test_a_truncated_text_answer_keeps_length(groq):
    out = await infer(groq(body("half an ans", finish_reason="length")))
    assert out.metadata["stop_reason"] == "length" and out.finish_reason == "length"


async def test_a_truncated_tool_call_keeps_length_while_finish_reason_is_unchanged(groq):
    out = await infer(groq(body(calls=[tool_call('{"path": "a')], finish_reason="length")))
    assert out.metadata["stop_reason"] == "length"
    assert out.finish_reason == "tool_calls"  # the existing, lossy behavior is untouched


@pytest.mark.parametrize("native", ["content_filter", "weird_new_reason"])
async def test_other_native_reasons_are_kept_verbatim_not_interpreted(groq, native):
    out = await infer(groq(body("x", finish_reason=native)))
    assert out.metadata["stop_reason"] == native and out.finish_reason == native


# ── nothing is synthesized ────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", [None, "", "   "])
async def test_a_null_or_blank_finish_reason_yields_no_stop_reason(groq, missing):
    out = await infer(groq(body("hello", finish_reason=missing)))
    assert "stop_reason" not in out.metadata
    assert out.finish_reason == (missing or "stop")  # the existing fallback, unchanged


async def test_a_tool_call_with_a_null_finish_reason_yields_no_stop_reason(groq):
    out = await infer(groq(body(calls=[tool_call()], finish_reason=None)))
    assert "stop_reason" not in out.metadata and out.finish_reason == "tool_calls"


def test_the_hook_reports_nothing_for_absent_or_non_string_fields():
    provider = GroqProvider(api_key=FAKE_KEY)
    for choice in (
        {},
        {"finish_reason": None},
        {"finish_reason": 5},
        {"finish_reason": ["stop"]},
        None,
    ):
        assert provider._response_metadata(choice, None) == {}  # type: ignore[arg-type]


async def test_a_response_with_no_choices_still_fails_as_before_and_carries_nothing(groq):
    with pytest.raises((KeyError, IndexError)):
        await infer(groq({"choices": [], "usage": {}}))


# ── contradictions are not turned into a completion signal ────────────────────


async def test_tool_calls_reason_without_any_call_is_not_preserved(groq):
    out = await infer(groq(body("text", finish_reason="tool_calls")))
    assert "stop_reason" not in out.metadata and out.finish_reason == "tool_calls"


@pytest.mark.parametrize("native", ["stop", "content_filter"])
async def test_a_call_under_a_contradicting_reason_is_not_preserved(groq, native):
    out = await infer(groq(body(calls=[tool_call()], finish_reason=native)))
    assert "stop_reason" not in out.metadata and out.finish_reason == "tool_calls"


async def test_the_reason_is_matched_case_insensitively_but_stored_as_received(groq):
    out = await infer(groq(body(calls=[tool_call()], finish_reason="Tool_Calls")))
    assert out.metadata["stop_reason"] == "Tool_Calls"


# ── nothing else changed ──────────────────────────────────────────────────────


async def test_the_request_sent_to_groq_is_unchanged(groq):
    provider = groq(body("x", finish_reason="stop"))
    await infer(provider)
    payload = json.loads(groq.sent[0].content)  # type: ignore[attr-defined]
    assert set(payload) == {"model", "messages", "temperature", "max_tokens", "top_p"}


async def test_other_openai_compatible_providers_still_report_no_metadata(monkeypatch):
    original = httpx.AsyncClient.__init__
    answer = body(calls=[tool_call()], finish_reason="tool_calls")

    def patched(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json=answer))
        return original(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    provider = OpenAIProvider(api_key=FAKE_KEY)
    out = await provider.infer(
        InferenceRequest(model_id="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    )
    await provider.shutdown()
    assert out.metadata == {} and out.finish_reason == "tool_calls"


def test_only_the_groq_adapter_overrides_the_hook():
    from velune.providers.adapters import openai_compat, xai

    assert GroqProvider._response_metadata is not OpenAIProvider._response_metadata
    assert (
        OpenAIProvider(api_key=FAKE_KEY)._response_metadata({"finish_reason": "stop"}, None) == {}
    )
    assert not hasattr(openai_compat, "_response_metadata") and not hasattr(
        xai, "_response_metadata"
    )
