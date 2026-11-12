"""LM Studio streaming: an error or a cut-off stream must never look like a finished tool call
(CodeRabbit #6 on PR #26).

Before, an error frame was silently skipped and the accumulated ``tool_calls`` fragments were
still published with ``finish_reason="tool_calls"``, so the agent could go on to run a
half-built ``write_file``. All tests use ``httpx.MockTransport``; no LM Studio server is needed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from velune.core.errors.provider import InferenceError
from velune.core.types.inference import InferenceRequest
from velune.providers.adapters.lmstudio import LMStudioProvider

WRITE_ARGS = json.dumps({"file_path": "out.txt", "content": "hello"})


def _frame(payload) -> str:
    return f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n"


def _tool_fragment(name="write_file", arguments=WRITE_ARGS, index=0, call_id="call_1"):
    function = {"arguments": arguments}
    if name:
        function["name"] = name
    return {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": index, "id": call_id, "type": "function", "function": function}
                    ]
                },
            }
        ]
    }


def _finish(reason):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def _text(piece):
    return {"choices": [{"index": 0, "delta": {"content": piece}}]}


ERROR_FRAME = {"error": {"message": "generation failed", "type": "server_error"}}
DONE = "data: [DONE]\n\n"


class _BrokenStream(httpx.AsyncByteStream):
    """Delivers some bytes, then the connection drops."""

    def __init__(self, first: bytes) -> None:
        self._first = first

    async def __aiter__(self):
        yield self._first
        raise httpx.ReadError("connection reset by peer")


def _provider(body: str | bytes | httpx.AsyncByteStream) -> LMStudioProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, httpx.AsyncByteStream):
            return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})
        content = body.encode() if isinstance(body, str) else body
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    provider = LMStudioProvider("http://lm.invalid/v1")
    provider.client = httpx.AsyncClient(
        base_url="http://lm.invalid/v1", transport=httpx.MockTransport(handler)
    )
    return provider


async def _collect(provider: LMStudioProvider):
    request = InferenceRequest(model_id="m", messages=[{"role": "user", "content": "go"}])
    chunks = []
    try:
        async for chunk in provider.stream(request):
            chunks.append(chunk)
    finally:
        await provider.shutdown()
    return chunks


def _published(chunks) -> list:
    return [call for chunk in chunks for call in ((chunk.metadata or {}).get("tool_calls") or [])]


async def _fails(body) -> list:
    """Run a stream that must fail; return the chunks seen before it did (never tool calls)."""
    seen: list = []
    provider = _provider(body)
    request = InferenceRequest(model_id="m", messages=[{"role": "user", "content": "go"}])
    with pytest.raises(InferenceError) as raised:
        async for chunk in provider.stream(request):
            seen.append(chunk)
    await provider.shutdown()
    assert _published(seen) == [], "no tool call may be published from a failed stream"
    return [str(raised.value)]


# ── Failures ──────────────────────────────────────────────────────────────────────────────────


async def test_a_tool_call_fragment_followed_by_an_error_frame_is_an_error():
    message = await _fails(_frame(_tool_fragment()) + _frame(ERROR_FRAME))
    assert "generation failed" in message[0]


async def test_the_same_with_a_done_sentinel_after_the_error_is_still_an_error():
    await _fails(_frame(_tool_fragment()) + _frame(ERROR_FRAME) + DONE)


async def test_a_bare_error_frame_is_an_error_not_silence():
    message = await _fails(_frame(ERROR_FRAME))
    assert "generation failed" in message[0]


async def test_a_string_error_payload_is_reported():
    message = await _fails(_frame({"error": "model unloaded"}))
    assert "model unloaded" in message[0]


async def test_an_event_error_line_marks_the_next_frame_as_an_error():
    await _fails("event: error\n" + _frame({"message": "boom"}))


async def test_a_finish_reason_of_error_is_an_error():
    await _fails(_frame(_tool_fragment()) + _frame(_finish("error")) + DONE)


async def test_a_truncated_tool_call_stream_is_an_error():
    """The connection simply stops: no finish_reason, no [DONE]."""
    await _fails(_frame(_tool_fragment()))


async def test_a_truncated_text_stream_is_an_error():
    await _fails(_frame(_text("partial answ")))


async def test_a_stream_cut_off_by_max_tokens_while_building_a_call_is_an_error():
    await _fails(
        _frame(_tool_fragment(arguments='{"file_path": "out.t')) + _frame(_finish("length")) + DONE
    )


async def test_a_unreadable_frame_while_building_tool_calls_is_an_error():
    await _fails(
        _frame(_tool_fragment())
        + _frame("{ this is not json")
        + _frame(_finish("tool_calls"))
        + DONE
    )


async def test_a_connection_that_drops_mid_stream_is_an_error():
    first = _frame(_tool_fragment()).encode()
    await _fails(_BrokenStream(first))


async def test_an_http_error_status_is_an_error():
    provider = LMStudioProvider("http://lm.invalid/v1")
    provider.client = httpx.AsyncClient(
        base_url="http://lm.invalid/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
    )
    with pytest.raises(InferenceError):
        await _collect(provider)


async def test_text_that_arrived_before_the_error_is_still_delivered_but_not_the_tool_call():
    provider = _provider(
        _frame(_text("Let me write that. ")) + _frame(_tool_fragment()) + _frame(ERROR_FRAME)
    )
    request = InferenceRequest(model_id="m", messages=[{"role": "user", "content": "go"}])
    seen = []
    with pytest.raises(InferenceError):
        async for chunk in provider.stream(request):
            seen.append(chunk)
    assert "".join(c.content for c in seen) == "Let me write that. "
    assert _published(seen) == []


async def test_the_provider_is_usable_again_after_a_failed_stream():
    """Nothing is left half-open or half-accumulated by the failure."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        body = (
            _frame(_tool_fragment()) + _frame(ERROR_FRAME)
            if state["calls"] == 1
            else _frame(_text("fine")) + _frame(_finish("stop")) + DONE
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    provider = LMStudioProvider("http://lm.invalid/v1")
    provider.client = httpx.AsyncClient(
        base_url="http://lm.invalid/v1", transport=httpx.MockTransport(handler)
    )
    request = InferenceRequest(model_id="m", messages=[{"role": "user", "content": "go"}])
    with pytest.raises(InferenceError):
        async for _ in provider.stream(request):
            pass
    chunks = [c async for c in provider.stream(request)]
    assert "".join(c.content for c in chunks) == "fine"
    assert _published(chunks) == []  # the first stream's fragment did not leak into this one
    await provider.shutdown()


# ── Normal behavior is preserved ─────────────────────────────────────────────────────────────


async def test_a_valid_tool_call_stream_publishes_the_call():
    chunks = await _collect(
        _provider(_frame(_tool_fragment()) + _frame(_finish("tool_calls")) + DONE)
    )
    (call,) = _published(chunks)
    assert call.name == "write_file"
    assert call.arguments == {"file_path": "out.txt", "content": "hello"}
    assert chunks[-1].finish_reason == "tool_calls"


async def test_fragmented_arguments_across_frames_are_assembled():
    half = len(WRITE_ARGS) // 2
    body = (
        _frame(_tool_fragment(arguments=WRITE_ARGS[:half]))
        + _frame(_tool_fragment(name=None, arguments=WRITE_ARGS[half:], call_id=""))
        + _frame(_finish("tool_calls"))
        + DONE
    )
    (call,) = _published(await _collect(_provider(body)))
    assert call.arguments == {"file_path": "out.txt", "content": "hello"}


async def test_two_tool_calls_in_one_reply_are_both_published():
    body = (
        _frame(_tool_fragment(index=0, call_id="a"))
        + _frame(
            _tool_fragment(name="read_file", arguments='{"file_path": "x"}', index=1, call_id="b")
        )
        + _frame(_finish("tool_calls"))
        + DONE
    )
    calls = _published(await _collect(_provider(body)))
    assert [c.name for c in calls] == ["write_file", "read_file"]


@pytest.mark.parametrize("reason", ["stop", "function_call"])
async def test_servers_that_finish_tool_calls_with_another_complete_reason_still_work(reason):
    chunks = await _collect(_provider(_frame(_tool_fragment()) + _frame(_finish(reason)) + DONE))
    assert [c.name for c in _published(chunks)] == ["write_file"]


async def test_done_without_a_finish_chunk_is_a_complete_stream():
    chunks = await _collect(_provider(_frame(_tool_fragment()) + DONE))
    assert [c.name for c in _published(chunks)] == ["write_file"]


async def test_a_finish_reason_without_done_is_a_complete_stream():
    chunks = await _collect(_provider(_frame(_text("hi")) + _frame(_finish("stop"))))
    assert "".join(c.content for c in chunks) == "hi"


async def test_a_normal_text_stream_is_unchanged():
    body = _frame(_text("Hel")) + _frame(_text("lo")) + _frame(_finish("stop")) + DONE
    chunks = await _collect(_provider(body))
    assert "".join(c.content for c in chunks) == "Hello"
    assert chunks[-1].finish_reason == "stop"
    assert _published(chunks) == []


async def test_a_usage_only_frame_and_data_without_a_space_are_tolerated():
    body = (
        f"data:{json.dumps(_text('ok'))}\n\n"
        + _frame({"choices": [], "usage": {"total_tokens": 5}})
        + _frame(_finish("stop"))
        + DONE
    )
    assert "".join(c.content for c in await _collect(_provider(body))) == "ok"


async def test_an_unreadable_frame_in_a_plain_text_stream_is_tolerated():
    body = (
        _frame(_text("a"))
        + _frame("{ broken")
        + _frame(_text("b"))
        + _frame(_finish("stop"))
        + DONE
    )
    assert "".join(c.content for c in await _collect(_provider(body))) == "ab"


async def test_a_text_reply_cut_off_by_length_is_reported_as_such_not_as_an_error():
    chunks = await _collect(_provider(_frame(_text("long")) + _frame(_finish("length")) + DONE))
    assert chunks[-1].finish_reason == "length"
