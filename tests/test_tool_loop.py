"""Contract tests for the native tool-calling loop and adapter wire helpers."""

from __future__ import annotations

import json
from typing import Any

import pytest

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.orchestration.tool_loop import (
    ToolLoopRunner,
    approve_readonly_only,
)
from velune.providers.adapters._toolcalls import (
    parse_ollama_tool_calls,
    parse_openai_tool_calls,
)
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission

# ── Fakes ───────────────────────────────────────────────────────────────────


class FakeProvider:
    """Scripted provider: returns queued responses in order."""

    def __init__(self, responses: list[InferenceResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[InferenceRequest] = []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        return self._responses.pop(0)


def _text_response(text: str) -> InferenceResponse:
    return InferenceResponse(
        content=text,
        model_id="fake",
        finish_reason="stop",
        tokens_used=10,
        latency_ms=1.0,
    )


def _tool_response(*calls: ToolCall) -> InferenceResponse:
    return InferenceResponse(
        content="",
        model_id="fake",
        finish_reason="tool_calls",
        tokens_used=10,
        latency_ms=1.0,
        tool_calls=list(calls),
    )


class EchoReadTool(BaseTool):
    """Read-only tool — auto-approved by the default policy."""

    def get_name(self) -> str:
        return "echo_read"

    def get_description(self) -> str:
        return "Echo back the given text"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str) -> str:
        return f"echo:{text}"


class WriteTool(BaseTool):
    """Write tool — denied by the default read-only approver."""

    executed = False

    def get_name(self) -> str:
        return "write_file"

    def get_description(self) -> str:
        return "Write a file"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    def get_schema(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> str:
        WriteTool.executed = True
        return "written"


def _registry(*tools: BaseTool) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


# ── Loop behavior ───────────────────────────────────────────────────────────


async def test_loop_executes_tool_and_returns_final_text():
    provider = FakeProvider(
        [
            _tool_response(ToolCall(id="c1", name="echo_read", arguments={"text": "hi"})),
            _text_response("done"),
        ]
    )
    runner = ToolLoopRunner(provider, _registry(EchoReadTool()))
    result = await runner.run(
        InferenceRequest(model_id="fake", messages=[{"role": "user", "content": "go"}])
    )

    assert result.content == "done"
    assert result.stop_reason == "completed"
    assert result.turns == 2
    assert len(result.invocations) == 1
    assert result.invocations[0].result == "echo:hi"
    assert not result.invocations[0].error

    # Second request must carry the assistant tool_calls echo + tool result.
    followup = provider.requests[1].messages
    assistant = followup[-2]
    tool_msg = followup[-1]
    assert assistant["role"] == "assistant"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"text": "hi"}
    assert tool_msg == {"role": "tool", "tool_call_id": "c1", "content": "echo:hi"}


async def test_default_approver_denies_write_tools_without_executing():
    WriteTool.executed = False
    provider = FakeProvider(
        [
            _tool_response(ToolCall(id="c1", name="write_file", arguments={"path": "x"})),
            _text_response("ok, I won't write"),
        ]
    )
    runner = ToolLoopRunner(provider, _registry(WriteTool()))
    result = await runner.run(
        InferenceRequest(model_id="fake", messages=[{"role": "user", "content": "write"}])
    )

    assert WriteTool.executed is False
    assert result.invocations[0].error
    assert "denied" in result.invocations[0].result
    # The denial is surfaced to the model as an error tool-result message.
    tool_msg = provider.requests[1].messages[-1]
    assert tool_msg["is_error"] is True


async def test_custom_approver_can_grant_write():
    WriteTool.executed = False

    async def allow_all(name, args, permissions):
        return True

    provider = FakeProvider(
        [
            _tool_response(ToolCall(id="c1", name="write_file", arguments={"path": "x"})),
            _text_response("done"),
        ]
    )
    runner = ToolLoopRunner(provider, _registry(WriteTool()), approver=allow_all)
    result = await runner.run(InferenceRequest(model_id="fake", messages=[]))
    assert WriteTool.executed is True
    assert result.invocations[0].result == "written"


async def test_unknown_tool_reports_error_to_model():
    provider = FakeProvider(
        [
            _tool_response(ToolCall(id="c1", name="nope", arguments={})),
            _text_response("adjusted"),
        ]
    )
    runner = ToolLoopRunner(provider, _registry(EchoReadTool()))
    result = await runner.run(InferenceRequest(model_id="fake", messages=[]))
    assert result.invocations[0].error
    assert "unknown tool" in result.invocations[0].result
    assert "echo_read" in result.invocations[0].result  # advertises what exists


async def test_max_turns_bound():
    call = ToolCall(id="c", name="echo_read", arguments={"text": "x"})
    provider = FakeProvider([_tool_response(call)] * 3)
    runner = ToolLoopRunner(provider, _registry(EchoReadTool()), max_turns=3)
    result = await runner.run(InferenceRequest(model_id="fake", messages=[]))
    assert result.stop_reason == "max_turns"
    assert result.turns == 3
    assert len(result.invocations) == 3


async def test_tool_exception_is_fed_back_not_raised():
    class BoomTool(EchoReadTool):
        def get_name(self) -> str:
            return "boom"

        async def execute(self, **kwargs: Any) -> str:
            raise ValueError("kaput")

    provider = FakeProvider(
        [
            _tool_response(ToolCall(id="c1", name="boom", arguments={})),
            _text_response("recovered"),
        ]
    )
    runner = ToolLoopRunner(provider, _registry(BoomTool()))
    result = await runner.run(InferenceRequest(model_id="fake", messages=[]))
    assert result.content == "recovered"
    assert result.invocations[0].error
    assert "kaput" in result.invocations[0].result


async def test_large_tool_output_is_truncated():
    class BigTool(EchoReadTool):
        def get_name(self) -> str:
            return "big"

        async def execute(self, **kwargs: Any) -> str:
            return "x" * 50_000

    provider = FakeProvider(
        [
            _tool_response(ToolCall(id="c1", name="big", arguments={})),
            _text_response("done"),
        ]
    )
    runner = ToolLoopRunner(provider, _registry(BigTool()), max_result_chars=1000)
    result = await runner.run(InferenceRequest(model_id="fake", messages=[]))
    assert len(result.invocations[0].result) < 1100
    assert "truncated" in result.invocations[0].result


async def test_no_tools_degrades_to_single_turn():
    provider = FakeProvider([_text_response("plain answer")])
    runner = ToolLoopRunner(provider, None)
    result = await runner.run(
        InferenceRequest(model_id="fake", messages=[{"role": "user", "content": "hi"}])
    )
    assert result.content == "plain answer"
    assert result.stop_reason == "no_tools"
    # No tools field must be sent to the provider at all.
    assert provider.requests[0].tools is None


def test_tool_definitions_are_openai_format():
    runner = ToolLoopRunner(FakeProvider([]), _registry(EchoReadTool()))
    defs = runner.build_tool_definitions()
    assert defs == [
        {
            "type": "function",
            "function": {
                "name": "echo_read",
                "description": "Echo back the given text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]


async def test_readonly_policy():
    assert await approve_readonly_only("t", {}, {ToolPermission.FILESYSTEM_READ})
    assert not await approve_readonly_only(
        "t", {}, {ToolPermission.FILESYSTEM_READ, ToolPermission.FILESYSTEM_WRITE}
    )
    assert not await approve_readonly_only("t", {}, {ToolPermission.NETWORK_ACCESS})
    assert not await approve_readonly_only("t", {}, set())  # no metadata → fail closed


# ── Adapter wire helpers ────────────────────────────────────────────────────


def test_parse_openai_tool_calls_json_string_args():
    msg = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path": "a.py"}'},
            }
        ],
    }
    calls = parse_openai_tool_calls(msg)
    assert calls is not None
    assert calls[0].id == "call_1"
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"file_path": "a.py"}


def test_parse_openai_tool_calls_malformed_args_kept():
    msg = {"tool_calls": [{"id": "c", "function": {"name": "t", "arguments": "{oops"}}]}
    calls = parse_openai_tool_calls(msg)
    assert calls is not None
    assert calls[0].arguments == {"_raw_arguments": "{oops"}


def test_parse_openai_tool_calls_absent():
    assert parse_openai_tool_calls({"content": "hello"}) is None
    assert parse_openai_tool_calls({"content": "hello", "tool_calls": []}) is None


def test_parse_ollama_tool_calls_synthesizes_ids():
    msg = {
        "content": "",
        "tool_calls": [{"function": {"name": "read_file", "arguments": {"file_path": "a.py"}}}],
    }
    calls = parse_ollama_tool_calls(msg)
    assert calls is not None
    assert calls[0].id.startswith("call_")
    assert calls[0].arguments == {"file_path": "a.py"}


# ── Anthropic translation ───────────────────────────────────────────────────


def test_anthropic_message_translation():
    pytest.importorskip("httpx")
    from velune.providers.adapters.anthropic import AnthropicProvider

    assistant = AnthropicProvider._translate_message(
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"file_path": "a.py"}'},
                }
            ],
        }
    )
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "text", "text": "thinking"}
    assert assistant["content"][1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "read_file",
        "input": {"file_path": "a.py"},
    }

    tool_result = AnthropicProvider._translate_message(
        {"role": "tool", "tool_call_id": "c1", "content": "data", "is_error": True}
    )
    assert tool_result == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "data", "is_error": True}
        ],
    }

    plain = AnthropicProvider._translate_message({"role": "user", "content": "hi"})
    assert plain == {"role": "user", "content": "hi"}


def test_anthropic_tool_definition_translation():
    from velune.providers.adapters.anthropic import AnthropicProvider

    out = AnthropicProvider._translate_tool(
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    )
    assert out == {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {"type": "object", "properties": {}},
    }
