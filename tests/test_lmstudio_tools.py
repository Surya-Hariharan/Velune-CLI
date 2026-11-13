"""LM Studio tool calling: tools reach the request, calls come back parsed.

LM Studio declares ``supports_function_calling``, so the REPL routes it through
the tool loop. Before this fix its adapter dropped ``request.tools`` from both
payloads and ignored ``tool_calls`` in responses, so the agent could never act.

These tests run against ``httpx.MockTransport`` (tests/http_fakes.py): they prove
the wire format, not a live LM Studio server.
"""

from __future__ import annotations

import json

import httpx

from tests.http_fakes import install_mock_transport
from velune.core.types.inference import InferenceRequest
from velune.providers.adapters.lmstudio import LMStudioProvider

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }
]


def _request(**extra) -> InferenceRequest:
    return InferenceRequest(
        model_id="qwen2.5-coder",
        messages=[{"role": "user", "content": "read README.md"}],
        tools=TOOLS,
        **extra,
    )


async def test_infer_attaches_tools_and_parses_tool_calls(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"file_path": "README.md"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"total_tokens": 12},
            },
        )

    seen = install_mock_transport(monkeypatch, handler)
    provider = LMStudioProvider()
    response = await provider.infer(_request(tool_choice="auto"))

    payload = json.loads(seen[0].content)
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"
    assert response.content == ""  # null content is not a crash
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls is not None
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"file_path": "README.md"}


async def test_infer_without_tools_sends_no_tools_key(monkeypatch):
    seen = install_mock_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        ),
    )
    response = await LMStudioProvider().infer(
        InferenceRequest(model_id="m", messages=[{"role": "user", "content": "hi"}])
    )
    assert "tools" not in json.loads(seen[0].content)
    assert response.content == "hi"
    assert response.tool_calls is None


async def test_failed_tool_result_flag_is_not_sent_to_the_server(monkeypatch):
    seen = install_mock_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        ),
    )
    request = InferenceRequest(
        model_id="m",
        messages=[
            {"role": "user", "content": "x"},
            {"role": "tool", "tool_call_id": "c1", "content": "Error: nope", "is_error": True},
        ],
        tools=TOOLS,
    )
    await LMStudioProvider().infer(request)
    assert all("is_error" not in m for m in json.loads(seen[0].content)["messages"])


def _sse(*events: dict) -> bytes:
    lines = [f"data: {json.dumps(e)}\n\n" for e in events]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


async def test_stream_accumulates_tool_call_fragments(monkeypatch):
    body = _sse(
        {"choices": [{"delta": {"content": "Let me look."}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_9",
                                "function": {"name": "read_file", "arguments": '{"file_'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'path": "a.py"}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    seen = install_mock_transport(
        monkeypatch,
        lambda r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}),
    )
    chunks = [c async for c in LMStudioProvider().stream(_request())]

    assert json.loads(seen[0].content)["tools"] == TOOLS
    assert "".join(c.content for c in chunks) == "Let me look."
    final = chunks[-1]
    assert final.finish_reason == "tool_calls"
    calls = final.metadata["tool_calls"]
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"file_path": "a.py"}


def test_lmstudio_opts_into_streamed_tool_calls():
    provider = LMStudioProvider()
    assert provider.SUPPORTS_STREAMING_TOOL_CALLS is True
    assert provider.get_capabilities().supports_function_calling is True
