"""A mode that says "critics disabled" must really skip them, and the tier
contract / trace verification must describe the run that actually happened."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from tests.council_fakes import CODE, REVIEW_FAIL, REVIEW_OK, healthy, make_orchestrator
from tests.test_council_phase0_characterization import _repl, _state, _StubOrchestrator
from velune.cli.handlers.council import _submit_background_job, cmd_council, cmd_run
from velune.cli.modes import SessionMode
from velune.cognition.budget import CouncilExecutionBudget
from velune.cognition.council.contracts import (
    ALL_CRITIC_SEATS,
    SEAT_CHALLENGER,
    SEAT_CODER,
    SEAT_PLANNER,
    SEAT_REVIEWER,
    SEAT_SYNTHESIZER,
    TIER_CONTRACTS,
    CouncilTier,
)

CRITICS = ("challenger", "scalability", "security", "performance", "maintainability")
FACTORY_CRITIC_METHODS = (
    "create_challenger",
    "create_scalability_critic",
    "create_security_critic",
    "create_performance_critic",
    "create_maintainability_critic",
)


# ── contract ─────────────────────────────────────────────────────────────────


def test_full_contract_without_critics_keeps_the_reviewer_and_the_pipeline():
    full = TIER_CONTRACTS[CouncilTier.FULL]
    reduced = full.without_critics()
    assert reduced.required_seats == (SEAT_PLANNER, SEAT_CODER, SEAT_REVIEWER, SEAT_SYNTHESIZER)
    assert reduced.critic_seats == ()
    assert not reduced.activates(SEAT_CHALLENGER)
    assert reduced.coder_samples == full.coder_samples
    assert reduced.min_provider_calls == full.min_provider_calls - 5


def test_without_critics_does_not_mutate_the_shared_contract():
    full = TIER_CONTRACTS[CouncilTier.FULL]
    before = full.required_seats
    full.without_critics()
    assert full.required_seats == before
    assert SEAT_CHALLENGER in full.required_seats
    assert all(seat in full.required_seats for seat in ALL_CRITIC_SEATS)


@pytest.mark.parametrize("tier", [CouncilTier.INSTANT, CouncilTier.MINIMAL, CouncilTier.STANDARD])
def test_tiers_without_critics_are_unchanged(tier):
    contract = TIER_CONTRACTS[tier]
    assert contract.without_critics() == contract


# ── orchestrator ─────────────────────────────────────────────────────────────


def _guard_critic_factories(orch):
    for name in FACTORY_CRITIC_METHODS:
        setattr(orch.agent_factory, name, MagicMock(side_effect=AssertionError(f"{name} called")))


async def test_disabled_critics_are_never_created_or_called(monkeypatch):
    orch, provider = make_orchestrator(monkeypatch)
    _guard_critic_factories(orch)
    result = await orch.execute_task(
        "explain it",
        "ctx",
        council_tier="full",
        budget=CouncilExecutionBudget(disable_critics=True),
    )
    seats = provider.seats_called()
    assert "reviewer" in seats
    for critic in CRITICS:
        assert critic not in seats
    for key in ("challenger_report", "scalability_report", "security_report"):
        assert result[key] is None
    assert result["reviewer_report"].usable
    assert result["contract_verdict"]["ok"] is True
    assert result["degraded"] is False


async def test_default_full_run_still_executes_every_judge(monkeypatch):
    orch, provider = make_orchestrator(monkeypatch)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    seats = provider.seats_called()
    for judge in ("reviewer", *CRITICS):
        assert judge in seats
    assert result["contract_verdict"]["ok"] is True


async def test_standard_is_identical_with_or_without_the_flag(monkeypatch):
    plain, plain_provider = make_orchestrator(monkeypatch)
    off, off_provider = make_orchestrator(monkeypatch)
    a = await plain.execute_task("explain it", "ctx", council_tier="standard")
    b = await off.execute_task(
        "explain it",
        "ctx",
        council_tier="standard",
        budget=CouncilExecutionBudget(disable_critics=True),
    )
    assert plain_provider.seats_called() == off_provider.seats_called()
    assert a["arbitration"]["overall_confidence"] == b["arbitration"]["overall_confidence"]
    assert b["contract_verdict"]["ok"] is True


async def test_the_milestone_is_announced_only_when_critics_were_actually_dropped(monkeypatch):
    budget = CouncilExecutionBudget(disable_critics=True)
    for tier, expect in (("full", True), ("standard", False)):
        orch, _ = make_orchestrator(monkeypatch)
        messages: list[str] = []
        await orch.execute_task(
            "explain it", "ctx", council_tier=tier, budget=budget, progress_callback=messages.append
        )
        assert any("critics disabled" in m for m in messages) is expect


async def test_debate_without_critics_is_a_single_reviewer_round(monkeypatch):
    counts = {"coder": 0, "reviewer": 0}

    def responder(seat, request):
        if seat == "coder":
            counts["coder"] += 1
            return CODE
        if seat == "reviewer":
            counts["reviewer"] += 1
            return REVIEW_FAIL if counts["reviewer"] == 1 else REVIEW_OK
        return healthy(seat, request)

    orch, provider = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task(
        "explain it",
        "ctx",
        council_tier="full",
        budget=CouncilExecutionBudget(disable_critics=True),
    )
    assert counts["coder"] == 4  # three diverge samples + one revision
    assert counts["reviewer"] == 2
    assert result["contract_verdict"]["ok"] is True


# ── commands ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(SessionMode.NORMAL, False), (SessionMode.OPTIMUS, True), (SessionMode.GODLY, False)],
)
async def test_run_and_council_pass_the_mode_flag(mode, expected):
    for command in (cmd_run, cmd_council):
        stub = _StubOrchestrator(_state())
        repl = _repl(stub)
        repl._mode_manager.set_mode(mode)
        await command(repl, "do it")
        assert stub.stream_kwargs["disable_critics"] is expected


async def test_explicit_council_tier_still_wins_but_the_mode_flag_still_applies():
    stub = _StubOrchestrator(_state())
    repl = _repl(stub)
    repl._mode_manager.set_mode(SessionMode.OPTIMUS)
    await cmd_council(repl, "do it")
    assert stub.stream_kwargs["council_tier"] == "full"
    assert stub.stream_kwargs["disable_critics"] is True


async def test_background_runs_pass_the_mode_flag():
    stub = _StubOrchestrator(_state())
    jobs: dict = {}

    class _Registry:
        def new_id(self):
            return "job1"

        def register(self, job):
            jobs[job.job_id] = job

        def update(self, job_id, **fields):
            for key, value in fields.items():
                setattr(jobs[job_id], key, value)

    repl = _repl(stub)
    repl._job_registry = _Registry()
    repl._mode_manager.set_mode(SessionMode.OPTIMUS)
    await _submit_background_job(repl, "do it", force_tier="instant")
    for _ in range(50):
        if stub.stream_kwargs:
            break
        await asyncio.sleep(0.01)
    assert stub.stream_kwargs["disable_critics"] is True
    assert stub.stream_kwargs["council_tier"] == "instant"
