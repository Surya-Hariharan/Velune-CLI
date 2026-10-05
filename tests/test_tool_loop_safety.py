"""Tool-loop safety: secrets never reach the model, failing calls aren't retried blindly,
and every permission decision is auditable."""

from __future__ import annotations

from pathlib import Path

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.permissions import ExecutionMode, PolicyState
from velune.permissions.boundary import Boundary
from velune.permissions.gate import PermissionGate
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission


class _Provider:
    provider_id = "fake"

    def __init__(self, turns: list[list[ToolCall]]) -> None:
        self._turns = [*turns, []]
        self.seen: list[list[dict]] = []

    async def get_capabilities(self):
        return None

    async def infer(self, request):
        self.seen.append(list(request.messages))
        calls = self._turns.pop(0) if self._turns else []
        return InferenceResponse(
            content="" if calls else "done",
            model_id="m",
            tokens_used=0,
            finish_reason="tool_calls" if calls else "stop",
            latency_ms=0.0,
            tool_calls=calls or None,
        )


class _LeakyRead(BaseTool):
    def get_name(self):
        return "leaky_read"

    def get_description(self):
        return ""

    def get_required_permissions(self):
        return {ToolPermission.FILESYSTEM_READ}

    async def execute(self, **kw):
        return "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCD"


class _AlwaysFails(BaseTool):
    runs = 0

    def get_name(self):
        return "flaky"

    def get_description(self):
        return ""

    def get_required_permissions(self):
        return {ToolPermission.FILESYSTEM_READ}

    async def execute(self, **kw):
        _AlwaysFails.runs += 1
        raise RuntimeError("missing import")


class _Cmd(BaseTool):
    def get_name(self):
        return "execute_command"

    def get_description(self):
        return ""

    def get_required_permissions(self):
        return {ToolPermission.FILESYSTEM_READ}

    async def execute(self, **kw):
        return {"exit_code": 3, "stdout": "", "stderr": "boom", "duration_ms": 1}


def _reg(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


async def _yes(*_):
    return True


def _call(i, name, **args):
    return ToolCall(id=f"c{i}", name=name, arguments=args)


async def test_secrets_in_tool_output_are_redacted_before_the_model_sees_them():
    provider = _Provider([[_call(1, "leaky_read")]])
    result = await ToolLoopRunner(provider, _reg(_LeakyRead()), approver=_yes).run(
        InferenceRequest(model_id="m", messages=[])
    )
    sent = str(provider.seen[-1])
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in sent
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in result.invocations[0].result


async def test_identical_failing_call_is_not_re_executed():
    _AlwaysFails.runs = 0
    same = {"path": "x"}
    provider = _Provider([[_call(1, "flaky", **same)], [_call(2, "flaky", **same)]])
    result = await ToolLoopRunner(provider, _reg(_AlwaysFails()), approver=_yes).run(
        InferenceRequest(model_id="m", messages=[])
    )
    assert _AlwaysFails.runs == 1  # the repeat was refused, not run again
    assert "not retried" in result.invocations[1].result


async def test_loop_stops_after_repeated_identical_failures():
    _AlwaysFails.runs = 0
    same = {"path": "x"}
    turns = [[_call(i, "flaky", **same)] for i in range(1, 7)]
    result = await ToolLoopRunner(
        _Provider(turns), _reg(_AlwaysFails()), approver=_yes, max_turns=10
    ).run(InferenceRequest(model_id="m", messages=[]))
    assert result.stop_reason == "repeated_failure"
    assert _AlwaysFails.runs == 1


async def test_changed_arguments_may_retry():
    _AlwaysFails.runs = 0
    provider = _Provider([[_call(1, "flaky", path="a")], [_call(2, "flaky", path="b")]])
    await ToolLoopRunner(provider, _reg(_AlwaysFails()), approver=_yes).run(
        InferenceRequest(model_id="m", messages=[])
    )
    assert _AlwaysFails.runs == 2


async def test_exit_code_reaches_the_event_stream():
    events = []
    provider = _Provider([[_call(1, "execute_command", command="pytest")]])
    await ToolLoopRunner(
        provider, _reg(_Cmd()), approver=_yes, on_event=lambda e, d: events.append((e, d))
    ).run(InferenceRequest(model_id="m", messages=[]))
    end = next(d for e, d in events if e == "tool_end")
    assert end["exit_code"] == 3
    assert end["arguments"] == {"command": "pytest"}


async def test_every_permission_decision_is_audited(tmp_path: Path):
    from velune.permissions.actions import Action, ActionType

    records = []
    gate = PermissionGate(
        PolicyState(mode=ExecutionMode.AUTO), Boundary(tmp_path), audit=records.append
    )
    await gate.check("write_file", [Action(ActionType.MODIFY_FILE, str(tmp_path / "a.py"))])
    record = records[0]
    assert record["mode"] == "auto"
    assert record["decision"] == "allow"
    assert record["actions"][0]["type"] == "MODIFY_FILE"
    assert record["tool"] == "write_file"
