"""M3: streaming tool events — accumulators and the runner's streamed turn."""

from __future__ import annotations

from types import SimpleNamespace

from velune.core.types.inference import InferenceRequest, StreamChunk, ToolCall
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.providers.adapters._toolcalls import (
    AnthropicStreamToolAccumulator,
    OpenAIStreamToolAccumulator,
)
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission

# ── OpenAI fragment accumulation ────────────────────────────────────────────


def test_openai_accumulator_joins_argument_fragments():
    acc = OpenAIStreamToolAccumulator()
    acc.add([{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": ""}}])
    acc.add([{"index": 0, "function": {"arguments": '{"file_'}}])
    acc.add([{"index": 0, "function": {"arguments": 'path": "a.py"}'}}])
    calls = acc.finalize()
    assert calls is not None
    assert calls[0].id == "call_1"
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"file_path": "a.py"}


def test_openai_accumulator_multiple_parallel_calls():
    acc = OpenAIStreamToolAccumulator()
    acc.add(
        [
            {"index": 0, "id": "a", "function": {"name": "t1", "arguments": "{}"}},
            {"index": 1, "id": "b", "function": {"name": "t2", "arguments": '{"x": 1}'}},
        ]
    )
    calls = acc.finalize()
    assert [c.name for c in calls] == ["t1", "t2"]
    assert calls[1].arguments == {"x": 1}


def test_openai_accumulator_empty_and_malformed():
    assert OpenAIStreamToolAccumulator().finalize() is None
    acc = OpenAIStreamToolAccumulator()
    acc.add([{"index": 0, "id": "c", "function": {"name": "t", "arguments": "{bad"}}])
    calls = acc.finalize()
    assert calls[0].arguments == {"_raw_arguments": "{bad"}


# ── Anthropic block accumulation ────────────────────────────────────────────


def test_anthropic_accumulator_tool_use_blocks():
    acc = AnthropicStreamToolAccumulator()
    acc.on_block_start(0, {"type": "text"})  # ignored
    acc.on_block_start(1, {"type": "tool_use", "id": "toolu_1", "name": "read_file"})
    acc.on_input_json_delta(1, '{"file_path"')
    acc.on_input_json_delta(1, ': "a.py"}')
    acc.on_input_json_delta(0, "ignored — not a tool_use block")
    calls = acc.finalize()
    assert calls is not None and len(calls) == 1
    assert calls[0].id == "toolu_1"
    assert calls[0].arguments == {"file_path": "a.py"}


def test_anthropic_accumulator_empty_input_means_no_args():
    acc = AnthropicStreamToolAccumulator()
    acc.on_block_start(0, {"type": "tool_use", "id": "t", "name": "noargs"})
    calls = acc.finalize()
    assert calls[0].arguments == {}


# ── Runner streamed turn ────────────────────────────────────────────────────


class EchoTool(BaseTool):
    def get_name(self) -> str:
        return "echo"

    def get_description(self) -> str:
        return "Echo"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "echoed"


class StreamingProvider:
    """Streams a tool-call turn, then a plain-text turn."""

    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(self) -> None:
        self.turn = 0
        self.infer_called = False

    def get_capabilities(self):
        return SimpleNamespace(supports_streaming=True, supports_function_calling=True)

    async def infer(self, request: InferenceRequest):
        self.infer_called = True
        raise AssertionError("streamed provider must not fall back to infer()")

    async def stream(self, request: InferenceRequest):
        self.turn += 1
        if self.turn == 1:
            yield StreamChunk(content="Let me check. ")
            yield StreamChunk(
                content="",
                finish_reason="tool_calls",
                metadata={"tool_calls": [ToolCall(id="c1", name="echo", arguments={})]},
            )
        else:
            for piece in ("final ", "answer"):
                yield StreamChunk(content=piece)
            yield StreamChunk(content="", finish_reason="stop")


async def test_streamed_loop_executes_tools_and_emits_deltas():
    events: list[tuple[str, dict]] = []
    registry = ToolRegistry()
    registry.register(EchoTool())
    provider = StreamingProvider()
    runner = ToolLoopRunner(provider, registry, on_event=lambda e, d: events.append((e, d)))
    result = await runner.run(
        InferenceRequest(model_id="m", messages=[{"role": "user", "content": "go"}])
    )

    assert result.content == "final answer"
    assert result.stop_reason == "completed"
    assert result.invocations[0].result == "echoed"
    assert provider.infer_called is False

    kinds = [e for e, _ in events]
    assert "content_delta" in kinds
    assert kinds.index("tool_start") > kinds.index("turn_end")  # spinner stopped first
    deltas = "".join(d.get("text", "") for e, d in events if e == "content_delta")
    assert "final answer" in deltas


class NonStreamMarkerProvider(StreamingProvider):
    """Supports streaming per capabilities, but no tool-call marker."""

    SUPPORTS_STREAMING_TOOL_CALLS = False

    async def infer(self, request: InferenceRequest):
        from velune.core.types.inference import InferenceResponse

        self.infer_called = True
        return InferenceResponse(
            content="plain", model_id="m", finish_reason="stop", tokens_used=1, latency_ms=1.0
        )


async def test_loop_never_streams_without_the_adapter_marker():
    registry = ToolRegistry()
    registry.register(EchoTool())
    provider = NonStreamMarkerProvider()
    runner = ToolLoopRunner(provider, registry)
    result = await runner.run(InferenceRequest(model_id="m", messages=[]))
    assert provider.infer_called is True  # fell back to blocking infer
    assert result.content == "plain"


def test_all_updated_adapters_declare_streaming_tool_support():
    from velune.providers.adapters.anthropic import AnthropicProvider
    from velune.providers.adapters.groq import GroqProvider
    from velune.providers.adapters.ollama import OllamaProvider
    from velune.providers.adapters.openai import OpenAIProvider
    from velune.providers.adapters.openai_compat import OpenAICompatProvider
    from velune.providers.adapters.openrouter import OpenRouterProvider

    for cls in (
        OpenAIProvider,
        GroqProvider,
        OpenRouterProvider,
        OpenAICompatProvider,
        OllamaProvider,
        AnthropicProvider,
    ):
        assert getattr(cls, "SUPPORTS_STREAMING_TOOL_CALLS", False) is True
