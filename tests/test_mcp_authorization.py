"""MCP tool calls go through authorize_and_execute and the PermissionGate (VAIR A1).

Uses the production ToolLoopRunner with a scripted provider and an in-process
fake MCP registry, so the test exercises the real routing, the real gate and
the real policy — only the remote server is faked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.mcp.authorization import MCPRegistryTool, describe_mcp_call, mcp_scope
from velune.mcp.transports.base import ToolInfo, tool_annotations
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.permissions.actions import ActionType, Risk, valid_scope
from velune.permissions.boundary import Boundary
from velune.permissions.gate import Approval, PermissionGate
from velune.permissions.policy import ExecutionMode, PolicyState
from velune.tools.base.tool import ToolCallContext, ToolPermission

# ── Fakes ───────────────────────────────────────────────────────────────────


class FakeMCPRegistry:
    def __init__(self, *tools: ToolInfo) -> None:
        self._tools = list(tools)
        self.calls: list[tuple[str, dict]] = []

    def all_tools(self) -> list[ToolInfo]:
        return list(self._tools)

    async def call_tool(self, qualified: str, arguments: dict[str, Any]) -> str:
        self.calls.append((qualified, arguments))
        return f"remote result for {qualified}"


class Scripted:
    def __init__(self, *responses: InferenceResponse) -> None:
        self._responses = list(responses)

    def get_capabilities(self):
        return SimpleNamespace(supports_streaming=False)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return self._responses.pop(0)


def _turn(*calls: ToolCall) -> InferenceResponse:
    return InferenceResponse(
        content="",
        model_id="m",
        finish_reason="tool_calls",
        tokens_used=1,
        latency_ms=1.0,
        tool_calls=list(calls),
    )


def _final(text="done") -> InferenceResponse:
    return InferenceResponse(
        content=text, model_id="m", finish_reason="stop", tokens_used=1, latency_ms=1.0
    )


def _tool(name="read_file", server="fs", **annotations) -> ToolInfo:
    return ToolInfo(
        name=name,
        description="d",
        input_schema={"type": "object", "properties": {}},
        server_name=server,
        annotations=annotations,
    )


async def _allow_all(name, args, perms):
    return True


async def _run(
    tmp_path: Path, mode: ExecutionMode, info: ToolInfo, *, ask=None, approver=_allow_all
):
    registry = FakeMCPRegistry(info)
    records: list[dict] = []
    gate = PermissionGate(
        state=PolicyState(mode=mode), boundary=Boundary(tmp_path), ask=ask, audit=records.append
    )
    ctx = ToolCallContext(run_id="r", actor="t", workspace=tmp_path, gate=gate)
    exposed = f"{info.server_name}_{info.name}"
    runner = ToolLoopRunner(
        Scripted(_turn(ToolCall(id="c1", name=exposed, arguments={"path": "a"})), _final()),
        None,
        mcp_registry=registry,
        approver=approver,
        ctx=ctx,
    )
    result = await runner.run(
        InferenceRequest(model_id="m", messages=[{"role": "user", "content": "x"}])
    )
    return result, registry, records


# ── Classification ──────────────────────────────────────────────────────────


def test_unannotated_mcp_tool_is_an_integration_execute():
    [action] = describe_mcp_call("fs", "write_file")
    assert action.action_type is ActionType.INTEGRATION_EXECUTE
    assert action.risk is Risk.MEDIUM
    assert action.scope == "mcp.fs.write_file" and valid_scope(action.scope)


def test_read_only_hint_cannot_lower_risk():
    [action] = describe_mcp_call("fs", "read_file", {"readOnlyHint": True})
    assert action.action_type is ActionType.INTEGRATION_EXECUTE


def test_destructive_hint_raises_risk():
    [action] = describe_mcp_call("fs", "rm", {"destructiveHint": True})
    assert action.action_type is ActionType.INTEGRATION_DELETE
    assert action.risk is Risk.HIGH


def test_hostile_names_still_produce_a_valid_scope():
    assert valid_scope(mcp_scope("Evil Server/../", "Drop*All"))


def test_tool_annotations_are_extracted_from_sdk_objects_and_dicts():
    sdk = SimpleNamespace(
        annotations=SimpleNamespace(
            model_dump=lambda exclude_none: {"destructiveHint": True, "title": "x"}
        )
    )
    assert tool_annotations(sdk) == {"destructiveHint": True}
    assert tool_annotations({"annotations": {"readOnlyHint": True}}) == {"readOnlyHint": True}
    assert tool_annotations({"annotations": "garbage"}) == {}
    assert tool_annotations(SimpleNamespace()) == {}


def test_registry_tool_reports_network_permission():
    tool = MCPRegistryTool(FakeMCPRegistry(), _tool(), "fs_read_file")
    assert tool.get_required_permissions() == {ToolPermission.NETWORK_ACCESS}


def test_client_wrapper_is_no_longer_a_local_read():
    from velune.mcp.client import MCPToolWrapper

    client = SimpleNamespace(server_name="fs", _connection=None)
    wrapper = MCPToolWrapper(client, "write_file", "d", {})
    [action] = wrapper.describe_actions({}, None)
    assert action.action_type is ActionType.INTEGRATION_EXECUTE
    assert wrapper.get_required_permissions() == {ToolPermission.NETWORK_ACCESS}


# ── Enforcement through the real loop ───────────────────────────────────────


async def test_plan_mode_refuses_mcp_calls_and_never_reaches_the_server(tmp_path):
    result, registry, records = await _run(tmp_path, ExecutionMode.PLAN, _tool())
    assert registry.calls == []
    assert result.invocations[0].error
    assert result.invocations[0].source == "mcp"
    assert records[-1]["decision"] == "deny"
    assert records[-1]["actions"][0]["scope"] == "mcp.fs.read_file"


async def test_auto_mode_runs_mcp_calls_and_audits_them(tmp_path):
    result, registry, records = await _run(tmp_path, ExecutionMode.AUTO, _tool())
    assert registry.calls == [("fs_read_file", {"path": "a"})]
    assert not result.invocations[0].error
    assert records[-1]["outcome"] == "allowed"
    assert records[-1]["tool"] == "fs_read_file"


async def test_manual_mode_without_a_human_refuses(tmp_path):
    _, registry, records = await _run(tmp_path, ExecutionMode.MANUAL, _tool())
    assert registry.calls == []
    assert "no interactive approval" in records[-1]["outcome"]


async def test_manual_mode_runs_after_the_user_approves(tmp_path):
    asked: list[str] = []

    async def ask(tool, decision):
        asked.append(tool)
        return Approval.ALLOW_ONCE

    _, registry, _ = await _run(tmp_path, ExecutionMode.MANUAL, _tool(), ask=ask)
    assert asked == ["fs_read_file"] and len(registry.calls) == 1


async def test_destructive_mcp_tool_needs_a_human_even_in_auto(tmp_path):
    _, registry, records = await _run(
        tmp_path, ExecutionMode.AUTO, _tool("drop_db", destructiveHint=True)
    )
    assert registry.calls == []
    assert records[-1]["actions"][0]["type"] == "INTEGRATION_DELETE"


async def test_default_approver_still_denies_mcp_calls(tmp_path):
    from velune.orchestration.tool_loop import approve_readonly_only

    _, registry, records = await _run(
        tmp_path, ExecutionMode.AUTO, _tool(), approver=approve_readonly_only
    )
    assert registry.calls == [] and records == []


async def test_without_a_gate_mcp_calls_fail_closed(tmp_path):
    registry = FakeMCPRegistry(_tool())
    runner = ToolLoopRunner(
        Scripted(_turn(ToolCall(id="c1", name="fs_read_file", arguments={})), _final()),
        None,
        mcp_registry=registry,
        approver=_allow_all,
        ctx=None,
    )
    result = await runner.run(
        InferenceRequest(model_id="m", messages=[{"role": "user", "content": "x"}])
    )
    assert registry.calls == [] and result.invocations[0].error


@pytest.mark.parametrize("mode", [ExecutionMode.MANUAL, ExecutionMode.AUTO])
async def test_tool_chat_approver_no_longer_special_cases_mcp(tmp_path, mode):
    """The REPL approver records arguments; the gate alone decides MCP calls."""
    from velune.cli.handlers.tool_chat import _make_approver

    ui = SimpleNamespace(pending_args={})
    approver = _make_approver(SimpleNamespace(), ui)
    assert await approver("fs_read_file", {"a": 1}, {ToolPermission.NETWORK_ACCESS})
    assert ui.pending_args["fs_read_file"] == {"a": 1}
