"""Tests for the REPL tool-chat wiring: budgets, windowing, gating, approval."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from velune.cli.handlers.tool_chat import (
    _make_approver,
    _ToolActivityUI,
    run_tool_chat,
    tool_loop_available,
)
from velune.cli.interrupts import InterruptController
from velune.cli.modes import SessionMode
from velune.context.budget import ContextBudget
from velune.core.errors.provider import InferenceError
from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.core.types.model import ModelDescriptor
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.safety import ApprovalMode

# ── Fixtures ────────────────────────────────────────────────────────────────


def _model(context_length: int = 32768, tool_use: Any = None) -> ModelDescriptor:
    caps = {}
    if tool_use is not None:
        caps["tool_use"] = tool_use
    return ModelDescriptor(
        model_id="test-model",
        provider_id="test",
        display_name="Test Model",
        context_length=context_length,
        capabilities=caps,
    )


class ReadTool(BaseTool):
    def get_name(self) -> str:
        return "peek"

    def get_description(self) -> str:
        return "Peek at a value"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "peeked"


class ScriptedProvider:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.requests: list[InferenceRequest] = []

    def get_capabilities(self):
        return SimpleNamespace(supports_function_calling=True)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _text(text: str) -> InferenceResponse:
    return InferenceResponse(
        content=text, model_id="test-model", finish_reason="stop", tokens_used=5, latency_ms=1.0
    )


def _tool_turn(*calls: ToolCall) -> InferenceResponse:
    return InferenceResponse(
        content="",
        model_id="test-model",
        finish_reason="tool_calls",
        tokens_used=5,
        latency_ms=1.0,
        tool_calls=list(calls),
    )


class FakeContainer:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services

    def get(self, key: str) -> Any:
        if key not in self._services:
            raise KeyError(key)
        return self._services[key]


def _fake_repl(tmp_path: Path, *, registry: ToolRegistry | None, native_tools: bool = True):
    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    services: dict[str, Any] = {"runtime.workspace": str(tmp_path)}
    if registry is not None:
        services["runtime.tool_registry"] = registry
    return SimpleNamespace(
        console=console,
        container=FakeContainer(services),
        runtime=SimpleNamespace(
            config=SimpleNamespace(
                execution=SimpleNamespace(native_tools=native_tools, max_tool_turns=5)
            )
        ),
        _approval_mode=ApprovalMode.ASK,
        _tools_unsupported_models=set(),
        _tool_session_grants=set(),
        _interrupts=InterruptController(),
        _mcp_registry=None,
        _hook_dispatcher=None,
        _session_id="testsess",
        _episodic_session_id=None,
        _tool_call_count=0,
    )


# ── M2: chat budgets ────────────────────────────────────────────────────────


def test_chat_budget_normal_mode_large_model():
    budget = ContextBudget.for_chat(SessionMode.NORMAL, 200_000)
    assert budget.total_tokens == 16384
    assert budget.output_reservation == 4096  # parity with the old hardcode
    assert budget.usable_tokens == 16384 - 4096 - 512


def test_chat_budget_small_local_model_never_overflows():
    budget = ContextBudget.for_chat(SessionMode.NORMAL, 4096)
    assert budget.total_tokens == 4096
    assert budget.output_reservation + budget.usable_tokens + 512 <= 4096
    assert budget.output_reservation >= 256
    assert budget.usable_tokens >= 128


def test_chat_budget_godly_uses_full_window():
    budget = ContextBudget.for_chat(SessionMode.GODLY, 200_000)
    assert budget.total_tokens == 200_000
    assert budget.output_reservation == 4096


def test_chat_budget_missing_context_length_falls_back():
    budget = ContextBudget.for_chat(SessionMode.NORMAL, 0)
    assert budget.total_tokens >= 1024
    assert budget.usable_tokens > 0


# ── M1: gating ──────────────────────────────────────────────────────────────


def test_gate_respects_config_off(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg(), native_tools=False)
    assert not tool_loop_available(repl, _model(), ScriptedProvider([]))


def test_gate_skips_models_marked_unsupported(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg())
    repl._tools_unsupported_models.add("test-model")
    assert not tool_loop_available(repl, _model(), ScriptedProvider([]))


def test_gate_requires_function_calling_capability(tmp_path):
    class NoToolsProvider(ScriptedProvider):
        def get_capabilities(self):
            return SimpleNamespace(supports_function_calling=False)

    repl = _fake_repl(tmp_path, registry=_reg())
    assert not tool_loop_available(repl, _model(), NoToolsProvider([]))


def test_gate_requires_warm_tool_registry(tmp_path):
    repl = _fake_repl(tmp_path, registry=None)
    assert not tool_loop_available(repl, _model(), ScriptedProvider([]))


def test_gate_open_when_all_conditions_met(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg())
    assert tool_loop_available(repl, _model(), ScriptedProvider([]))


def _reg() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadTool())
    return reg


# ── M1: end-to-end chat turn through the loop ───────────────────────────────


async def test_run_tool_chat_happy_path(tmp_path):
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCall(id="c1", name="peek", arguments={})),
            _text("the answer"),
        ]
    )
    repl = _fake_repl(tmp_path, registry=_reg())
    request = InferenceRequest(
        model_id="test-model", messages=[{"role": "user", "content": "look"}]
    )
    result = await run_tool_chat(repl, _model(), provider, request)
    assert result is not None
    assert result.content == "the answer"
    assert result.invocations[0].result == "peeked"
    assert repl._tool_call_count == 1


async def test_run_tool_chat_falls_back_on_first_turn_rejection(tmp_path):
    provider = ScriptedProvider([InferenceError("400: tools not supported")])
    repl = _fake_repl(tmp_path, registry=_reg())
    request = InferenceRequest(model_id="test-model", messages=[{"role": "user", "content": "hi"}])
    result = await run_tool_chat(repl, _model(), provider, request)
    assert result is None  # caller falls back to legacy streaming
    assert "test-model" in repl._tools_unsupported_models


async def test_run_tool_chat_returns_none_when_gated_off(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg(), native_tools=False)
    request = InferenceRequest(model_id="test-model", messages=[])
    assert await run_tool_chat(repl, _model(), ScriptedProvider([]), request) is None


# ── M1: approval policy ─────────────────────────────────────────────────────


def _ui(repl) -> _ToolActivityUI:
    return _ToolActivityUI(repl)


async def test_approver_auto_allows_readonly(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg())
    approver = _make_approver(repl, _ui(repl))
    assert await approver("peek", {}, {ToolPermission.FILESYSTEM_READ})
    assert await approver("blame", {}, {ToolPermission.GIT_READ})


async def test_approver_block_mode_denies_everything_mutating(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg())
    repl._approval_mode = ApprovalMode.BLOCK
    approver = _make_approver(repl, _ui(repl))
    assert not await approver("write_file", {}, {ToolPermission.FILESYSTEM_WRITE})
    assert not await approver(
        "execute_command", {"command": "ls"}, {ToolPermission.TERMINAL_EXECUTE}
    )
    # Read-only still allowed even in block mode.
    assert await approver("peek", {}, {ToolPermission.FILESYSTEM_READ})


async def test_approver_blocks_dangerous_commands_without_prompting(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg())
    approver = _make_approver(repl, _ui(repl))
    assert not await approver(
        "execute_command", {"command": "rm -rf /"}, {ToolPermission.TERMINAL_EXECUTE}
    )


async def test_approver_safe_mode_autoruns_safe_commands(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg())
    repl._approval_mode = ApprovalMode.SAFE
    approver = _make_approver(repl, _ui(repl))
    assert await approver(
        "execute_command", {"command": "git status"}, {ToolPermission.TERMINAL_EXECUTE}
    )


async def test_approver_honors_session_grants(tmp_path):
    repl = _fake_repl(tmp_path, registry=_reg())
    repl._tool_session_grants.add("write_file")
    approver = _make_approver(repl, _ui(repl))
    assert await approver("write_file", {"path": "x"}, {ToolPermission.FILESYSTEM_WRITE})
