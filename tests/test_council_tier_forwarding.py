"""An explicit council tier must reach the orchestrator, and a capped one must say so."""

from __future__ import annotations

import asyncio
import logging

import pytest
import typer

from tests.council_fakes import make_orchestrator
from tests.test_council_phase0_characterization import _repl, _state, _StubOrchestrator
from velune.cli.commands.ask import _validate_council_tier
from velune.cli.handlers.council import (
    _mode_council_tier,
    _submit_background_job,
    cmd_council,
    cmd_run,
)
from velune.cli.modes import ModeManager, SessionMode
from velune.cognition.council.tiers import CouncilTier, classify_task_tier, parse_tier

# ── stream() forwards the tier ───────────────────────────────────────────────


async def _captured_execute_kwargs(orch, monkeypatch, **stream_kwargs):
    captured: dict = {}

    async def fake_execute_task(*args, **kwargs):
        captured.update(kwargs)
        return {"final_summary": "ok", "task_plan": None, "coder_proposal": None}

    monkeypatch.setattr(orch, "execute_task", fake_execute_task)
    async for _ in orch.stream("task", **stream_kwargs):
        pass
    return captured


async def test_stream_forwards_the_requested_tier(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    captured = await _captured_execute_kwargs(orch, monkeypatch, council_tier="full")
    assert captured["council_tier"] == "full"


async def test_stream_defaults_to_automatic_classification(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    captured = await _captured_execute_kwargs(orch, monkeypatch)
    assert captured["council_tier"] is None


# ── commands pass the tier they promise ──────────────────────────────────────


async def test_council_command_forces_the_full_tier():
    stub = _StubOrchestrator(_state())
    await cmd_council(_repl(stub), "do it")
    assert stub.stream_kwargs["council_tier"] == "full"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(SessionMode.NORMAL, None), (SessionMode.OPTIMUS, "instant"), (SessionMode.GODLY, "full")],
)
async def test_run_command_uses_the_session_mode_tier(mode, expected):
    stub = _StubOrchestrator(_state())
    repl = _repl(stub)
    repl._mode_manager.set_mode(mode)
    assert _mode_council_tier(repl) == expected
    await cmd_run(repl, "do it")
    assert stub.stream_kwargs["council_tier"] == expected


async def test_background_run_forwards_the_tier_too():
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
    await _submit_background_job(repl, "do it", force_tier="full")
    for _ in range(50):
        if stub.stream_kwargs:
            break
        await asyncio.sleep(0.01)
    assert stub.stream_kwargs["council_tier"] == "full"


async def test_run_command_background_flag_uses_the_mode_tier():
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
    repl._mode_manager = ModeManager()
    repl._mode_manager.set_mode(SessionMode.GODLY)
    await cmd_run(repl, "do it --bg")
    for _ in range(50):
        if stub.stream_kwargs:
            break
        await asyncio.sleep(0.01)
    assert stub.stream_kwargs["council_tier"] == "full"


# ── caps are announced ───────────────────────────────────────────────────────


async def test_forced_tier_capped_by_the_ceiling_is_announced(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    orch.tier_classifier.max_council_tier = "standard"
    messages: list[str] = []
    result = await orch.execute_task(
        "explain it", "ctx", council_tier="full", progress_callback=messages.append
    )
    assert result["tier"] == "standard"
    assert "[Council] Requested FULL tier, running STANDARD (ceiling is standard)" in messages


async def test_forced_tier_demoted_by_low_resource_mode_is_announced(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    orch.tier_classifier.low_resource_mode = True
    messages: list[str] = []
    result = await orch.execute_task(
        "explain it", "ctx", council_tier="full", progress_callback=messages.append
    )
    assert result["tier"] == "standard"
    assert any("low-resource mode" in m for m in messages)


async def test_honoured_tier_is_not_announced(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    messages: list[str] = []
    result = await orch.execute_task(
        "explain it", "ctx", council_tier="full", progress_callback=messages.append
    )
    assert result["tier"] == "full"
    assert not any(m.startswith("[Council] Requested") for m in messages)


async def test_automatic_classification_is_unchanged(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    messages: list[str] = []
    result = await orch.execute_task("explain it", "ctx", progress_callback=messages.append)
    assert result["tier"] == "instant"  # "explain" is a read-only request
    assert not any(m.startswith("[Council] Requested") for m in messages)


# ── validation ───────────────────────────────────────────────────────────────


def test_parse_tier_accepts_every_tier_and_auto():
    for tier in CouncilTier:
        assert parse_tier(tier.value) is tier
        assert parse_tier(tier.value.upper()) is tier
    assert parse_tier("auto") is None
    assert parse_tier(None) is None
    assert parse_tier("  ") is None


def test_parse_tier_rejects_junk_and_names_the_choices():
    with pytest.raises(ValueError) as excinfo:
        parse_tier("bogus")
    for name in ("instant", "minimal", "standard", "full", "auto"):
        assert name in str(excinfo.value)


def test_ask_council_tier_option_validates():
    assert _validate_council_tier("FULL") == "full"
    assert _validate_council_tier("auto") is None
    assert _validate_council_tier(None) is None
    with pytest.raises(typer.BadParameter):
        _validate_council_tier("bogus")


def test_unknown_override_is_logged_not_silently_ignored(caplog):
    with caplog.at_level(logging.WARNING, logger="velune.cognition.council.tiers"):
        tier = classify_task_tier("explain how this works", "", default_tier_override="bogus")
    assert tier is CouncilTier.INSTANT  # fell back to automatic classification
    assert "bogus" in caplog.text
