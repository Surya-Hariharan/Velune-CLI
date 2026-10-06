"""Phase 0 characterization tests: each asserts the *desired* behaviour and is a
strict xfail until the fix lands, so the defect is executable evidence rather
than prose. Every fix commit removes the marker for its group.

Each group also has a healthy-path companion that is NOT xfail, so a broken
harness cannot masquerade as a reproduced defect.
"""

from __future__ import annotations

import contextlib
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from rich.console import Console

from tests.council_fakes import (
    CODE,
    FINAL,
    REVIEW_FAIL,
    REVIEW_OK,
    FakeProvider,
    healthy,
    make_model,
    make_orchestrator,
)
from velune.cognition.budget import CouncilExecutionBudget
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.orchestration.schemas import ExecutionStatus, OrchestrationRequest, OrchestrationState


def xfail(reason: str):
    return pytest.mark.xfail(strict=True, reason=reason)


# ── harness sanity ───────────────────────────────────────────────────────────


async def test_healthy_standard_run_uses_four_seats(monkeypatch):
    orch, provider = make_orchestrator(monkeypatch)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert provider.seats_called() == ["planner", "coder", "reviewer", "synthesizer"]
    assert result["final_summary"] == FINAL
    assert result["arbitration"]["requires_human_review"] is False


async def test_healthy_full_run_uses_all_judges(monkeypatch):
    orch, provider = make_orchestrator(monkeypatch)
    await orch.execute_task("explain it", "ctx", council_tier="full")
    seats = provider.seats_called()
    for judge in (
        "reviewer",
        "challenger",
        "scalability",
        "security",
        "performance",
        "maintainability",
    ):
        assert judge in seats


# ── Issue 2: failed judges are not approvals ─────────────────────────────────


async def test_failed_reviewer_is_not_an_approval(monkeypatch):
    def responder(seat, request):
        if seat == "reviewer":
            return RuntimeError("provider down")
        return healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    arbitration = result["arbitration"]
    assert arbitration["requires_human_review"] is True
    assert any(f.startswith("JUDGE_UNAVAILABLE") for f in arbitration["flags"])


async def test_failed_critic_is_not_an_approval(monkeypatch):
    def responder(seat, request):
        if seat == "security":
            return RuntimeError("provider down")
        return healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    flags = result["arbitration"]["flags"]
    assert any(f.startswith("JUDGE_UNAVAILABLE") and "security" in f for f in flags)


async def test_failed_debate_revision_keeps_previous_proposal(monkeypatch):
    state = {"coder": 0, "reviewer": 0}

    def responder(seat, request):
        if seat == "coder":
            state["coder"] += 1
            return CODE if state["coder"] == 1 else RuntimeError("provider down")
        if seat == "reviewer":
            state["reviewer"] += 1
            return REVIEW_FAIL if state["reviewer"] == 1 else REVIEW_OK
        return healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert result["coder_proposal"] == CODE


# ── Issue 3: synthesizer failure / hard failure are not successful answers ───


async def test_synthesizer_timeout_is_not_the_final_answer(monkeypatch):
    def responder(seat, request):
        if seat == "synthesizer":
            return TimeoutError()
        return healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert result.get("degraded") is True
    assert result["final_summary"].startswith("# Council Deliberation Report (Degraded Mode)")
    assert "using empty response" not in result["final_summary"]
    assert not result["final_summary"].startswith("[Agent")


def _stream_state_for(orch, monkeypatch, result):
    async def fake_execute_task(*args, **kwargs):
        return result

    monkeypatch.setattr(orch, "execute_task", fake_execute_task)

    async def run():
        run_id = None
        async for milestone in orch.stream("task"):
            run_id = milestone.run_id
        return orch.get_state(run_id)

    return run()


async def test_healthy_result_is_completed(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    state = await _stream_state_for(
        orch, monkeypatch, {"final_summary": "ok", "task_plan": None, "coder_proposal": None}
    )
    assert state.status == ExecutionStatus.COMPLETED


async def test_hard_failure_result_is_marked_failed(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    state = await _stream_state_for(orch, monkeypatch, orch._build_timeout_result("task"))
    assert state.status == ExecutionStatus.FAILED


class _StubOrchestrator:
    """Minimal orchestrator for the REPL handler: records how stream() is called."""

    def __init__(self, state):
        self.state = state
        self.stream_kwargs: dict = {}

    async def stream(self, prompt, *args, **kwargs):
        self.stream_kwargs = kwargs
        yield SimpleNamespace(run_id="r1", phase="council", message="working", elapsed_ms=None)

    def get_state(self, run_id):
        return self.state


def _state(status=ExecutionStatus.COMPLETED, output="the answer", error=None):
    return OrchestrationState(
        run_id="r1",
        request=OrchestrationRequest(prompt="p", workspace="."),
        status=status,
        output=output,
        error=error,
    )


def _repl(orchestrator):
    from velune.cli.modes import ModeManager

    buffer = StringIO()

    @contextlib.asynccontextmanager
    async def foreground():
        yield

    services = {
        "runtime.council_orchestrator": orchestrator,
        "runtime.repository_cognition": MagicMock(),
    }
    repl = SimpleNamespace(
        console=Console(file=buffer, width=100, color_system=None),
        container=SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services),
        _interrupts=SimpleNamespace(foreground=foreground, consume_user_cancelled=lambda: False),
        _conversation=[],
        _mode_manager=ModeManager(),
        _job_registry=None,
    )
    repl.output = buffer
    return repl


async def test_repl_stores_a_healthy_council_answer():
    from velune.cli.handlers.council import execute_council_task

    repl = _repl(_StubOrchestrator(_state()))
    await execute_council_task(repl, "do it", force_tier=None)
    assert repl._conversation[-1]["content"] == "the answer"


async def test_repl_does_not_store_a_failed_run():
    from velune.cli.handlers.council import execute_council_task

    failed = _state(ExecutionStatus.FAILED, output="Execution failed: boom", error="boom")
    repl = _repl(_StubOrchestrator(failed))
    await execute_council_task(repl, "do it", force_tier=None)
    assert repl._conversation == []
    assert "Council Result" not in repl.output.getvalue()


# ── Issue 1: tier forwarding ─────────────────────────────────────────────────


async def test_stream_forwards_the_requested_tier(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    captured: dict = {}

    async def fake_execute_task(*args, **kwargs):
        captured.update(kwargs)
        return {"final_summary": "ok", "task_plan": None, "coder_proposal": None}

    monkeypatch.setattr(orch, "execute_task", fake_execute_task)
    async for _ in orch.stream("task", council_tier="full"):
        pass
    assert captured.get("council_tier") == "full"


async def test_council_command_forces_the_full_tier():
    from velune.cli.handlers.council import cmd_council

    stub = _StubOrchestrator(_state())
    await cmd_council(_repl(stub), "do it")
    assert stub.stream_kwargs.get("council_tier") == "full"


# ── Issue 5: disable_critics ─────────────────────────────────────────────────


async def test_disable_critics_skips_challenger_and_critics(monkeypatch):
    orch, provider = make_orchestrator(monkeypatch)
    await orch.execute_task(
        "explain it",
        "ctx",
        council_tier="full",
        budget=CouncilExecutionBudget(disable_critics=True),
    )
    seats = provider.seats_called()
    assert "reviewer" in seats
    for critic in ("challenger", "scalability", "security", "performance", "maintainability"):
        assert critic not in seats


# ── Issue 4: deterministic per-agent fallback ────────────────────────────────


async def test_reviewer_falls_back_to_the_configured_provider(monkeypatch):
    from velune.core.errors.provider import ProviderConnectionError
    from velune.kernel.config import VeluneConfig
    from velune.models.specializations import ModelSpecializationMapper

    primary_model = make_model("m-primary", "p1")
    backup_model = make_model("m-backup", "p2")

    class Registry:
        def list_all(self):
            return [primary_model, backup_model]

        def get(self, model_id, provider_id=None):
            return next((m for m in self.list_all() if m.model_id == model_id), None)

    def p1_responder(seat, request):
        if seat == "reviewer":
            return ProviderConnectionError("p1 down")
        return healthy(seat, request)

    p1 = FakeProvider("p1", p1_responder)
    p2 = FakeProvider("p2")
    config = VeluneConfig()
    config.providers.fallback_providers = ["p2"]
    orch, _ = make_orchestrator(
        monkeypatch,
        providers=[p1, p2],
        mapper=ModelSpecializationMapper(Registry()),
        config=config,
    )
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert p2.seats_called() == ["reviewer"]
    assert not any(f.startswith("JUDGE_UNAVAILABLE") for f in result["arbitration"]["flags"])


# ── Issue 6: /roles ──────────────────────────────────────────────────────────


def test_inert_roles_cannot_be_assigned():
    from velune.orchestration.role_assignments import CouncilRoleMap

    role_map = CouncilRoleMap()
    for role in ("embedding", "architect", "security"):
        with pytest.raises(ValueError):
            role_map.assign(role, "m", "p")


def test_one_bad_entry_does_not_discard_the_rest(tmp_path):
    import json

    from velune.orchestration.role_assignments import CouncilRoleMap

    path = tmp_path / "council_roles.json"
    path.write_text(
        json.dumps(
            {
                "coder": {"model_id": "m", "provider_id": "p"},
                "not-a-role": {"model_id": "x", "provider_id": "y"},
            }
        ),
        encoding="utf-8",
    )
    loaded = CouncilRoleMap.load(path)
    assert loaded.get("coder") is not None


async def test_picker_offers_exactly_the_wired_roles(monkeypatch):
    from velune.cli import councilmodel_ui
    from velune.cli.interactive import CANCEL
    from velune.orchestration.role_assignments import CouncilRoleMap

    offered: list[str] = []

    async def fake_select(title, options, **kwargs):
        offered.extend(o.id for o in options)
        return CANCEL

    monkeypatch.setattr(councilmodel_ui, "single_select", fake_select)
    await councilmodel_ui.run_councilmodel_ui(
        CouncilRoleMap(), [], Console(file=StringIO(), color_system=None)
    )
    assert offered == ["planner", "coder", "reviewer", "challenger", "synthesizer"]


# ── Issue 7: MCP council ─────────────────────────────────────────────────────


@xfail("Issue 7: the MCP server cannot be given an orchestrator and calls a missing .run()")
async def test_mcp_velune_ask_runs_the_real_council_api():
    from velune.mcp.server import VeluneMCPServer

    stub = create_autospec(CouncilOrchestrator, instance=True)
    stub.execute_task = AsyncMock(
        return_value={
            "final_summary": "council says hi",
            "arbitration": {"overall_confidence": 0.8, "flags": []},
        }
    )
    server = VeluneMCPServer(council_orchestrator=stub)
    result = await server._velune_ask("hello")
    assert result["response"] == "council says hi"
