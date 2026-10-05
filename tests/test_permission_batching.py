"""MANUAL mode confirms a turn's related changes with one prompt, not one per edit."""

from __future__ import annotations

from pathlib import Path

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.permissions import ExecutionMode, PolicyState
from velune.permissions.boundary import Boundary
from velune.permissions.gate import Approval, PermissionGate
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import ToolCallContext
from velune.tools.filesystem.dirs import CreateDirectory, DeleteDirectory
from velune.tools.filesystem.write import WriteFile


class _Provider:
    """Scripted provider: one turn of tool calls, then a final answer."""

    provider_id = "fake"

    def __init__(self, calls: list[ToolCall]) -> None:
        self._turns = [calls, []]

    async def get_capabilities(self):
        return None

    async def infer(self, request):
        calls = self._turns.pop(0)
        return InferenceResponse(
            content="" if calls else "done",
            model_id="m",
            tokens_used=0,
            finish_reason="tool_calls" if calls else "stop",
            latency_ms=0.0,
            tool_calls=calls or None,
        )


def _call(i: int, name: str, **args) -> ToolCall:
    return ToolCall(id=f"c{i}", name=name, arguments=args)


def _registry(ws: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(WriteFile(workspace=ws, confirm=False))
    reg.register(CreateDirectory(workspace=ws))
    reg.register(DeleteDirectory(workspace=ws))
    return reg


async def _run(ws: Path, calls: list[ToolCall], *, batch_answer: bool, single_answer=Approval.DENY):
    batches: list[list] = []
    singles: list = []

    async def ask_batch(items):
        batches.append(items)
        return batch_answer

    async def ask(tool_name, decision):
        singles.append((tool_name, decision))
        return single_answer

    gate = PermissionGate(PolicyState(mode=ExecutionMode.MANUAL), Boundary(ws), ask=ask)
    gate.ask_batch = ask_batch
    ctx = ToolCallContext(run_id="t", actor="t", workspace=ws, gate=gate)

    async def approver(name, args, perms):
        return True

    runner = ToolLoopRunner(_Provider(calls), _registry(ws), approver=approver, ctx=ctx)
    result = await runner.run(InferenceRequest(model_id="m", messages=[]))
    return result, batches, singles


async def test_related_changes_are_approved_with_one_prompt(tmp_path):
    calls = [
        _call(1, "write_file", file_path="src/a.py", content="a"),
        _call(2, "write_file", file_path="src/b.py", content="b"),
        _call(3, "create_directory", path="tests"),
    ]
    result, batches, singles = await _run(tmp_path, calls, batch_answer=True)
    assert len(batches) == 1 and len(batches[0]) == 3
    assert singles == []  # nothing asked twice
    assert (tmp_path / "src" / "a.py").read_text() == "a"
    assert (tmp_path / "tests").is_dir()
    assert not any(inv.error for inv in result.invocations)


async def test_rejecting_the_batch_changes_nothing(tmp_path):
    calls = [
        _call(1, "write_file", file_path="a.py", content="a"),
        _call(2, "write_file", file_path="b.py", content="b"),
    ]
    result, batches, singles = await _run(tmp_path, calls, batch_answer=False)
    assert len(batches) == 1 and singles == []
    assert not (tmp_path / "a.py").exists() and not (tmp_path / "b.py").exists()
    assert all("rejected" in inv.result for inv in result.invocations)


async def test_high_risk_is_never_folded_into_the_batch(tmp_path):
    big = tmp_path / "old"
    big.mkdir()
    (big / "f.txt").write_text("x")
    calls = [
        _call(1, "write_file", file_path="a.py", content="a"),
        _call(2, "write_file", file_path="b.py", content="b"),
        _call(3, "delete_directory", path="old", recursive=True),
    ]
    result, batches, singles = await _run(tmp_path, calls, batch_answer=True)
    assert len(batches) == 1 and len(batches[0]) == 2  # only the ordinary writes
    assert len(singles) == 1 and singles[0][1].high_risk  # separate ⚠ prompt
    assert big.exists()  # single prompt answered "deny"
    assert (tmp_path / "a.py").exists()


async def test_a_single_change_uses_the_normal_prompt(tmp_path):
    calls = [_call(1, "write_file", file_path="a.py", content="a")]
    _, batches, singles = await _run(
        tmp_path, calls, batch_answer=True, single_answer=Approval.ALLOW_ONCE
    )
    assert batches == []
    assert len(singles) == 1
