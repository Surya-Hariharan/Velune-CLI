"""Tests for the multi-solver diverge round + self-consistency selection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from velune.cognition.consensus import medoid_index
from velune.cognition.council.scheduler import CouncilScheduler
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.models.specializations import CouncilRole, detect_model_collapse


class FakeCoder:
    """Minimal coder stub: returns text keyed by sample temperature."""

    def __init__(self, by_temp: dict[float, str], *, fail_on: set[float] | None = None) -> None:
        self.model = SimpleNamespace(provider_id="ollama")
        self._by_temp = by_temp
        self._fail_on = fail_on or set()
        self.temps_seen: list[float] = []

    async def write_code(self, *, temperature=None, **_: object) -> str:
        self.temps_seen.append(temperature)
        if temperature in self._fail_on:
            raise RuntimeError("sample failed")
        return self._by_temp[temperature]


def _run_diverge(coder: FakeCoder, n: int) -> list[str]:
    # _diverge_candidates only touches self.scheduler, so a light stand-in works.
    holder = SimpleNamespace(scheduler=CouncilScheduler())
    coro = CouncilOrchestrator._diverge_candidates(
        holder,
        coder,
        prompt="p",
        current_code="c",
        plan_context="plan",
        style_profile=None,
        format_instructions="",
        n_samples=n,
        timeout=5.0,
    )
    return asyncio.run(coro)


def test_diverge_draws_distinct_temperatures() -> None:
    coder = FakeCoder({0.2: "A", 0.5: "B", 0.8: "C"})
    candidates = _run_diverge(coder, 3)
    assert len(candidates) == 3
    assert len(set(coder.temps_seen)) == 3  # genuinely divergent samples


def test_self_consistency_picks_the_consensus_candidate() -> None:
    # Two near-identical solutions + one outlier; medoid must pick the agreeing pair.
    text_a = "def add(a, b):\n    return a + b\n"
    text_b = "def add(a, b):\n    return a + b  # sum\n"
    text_c = "import os\nprint('totally different program')\n"
    coder = FakeCoder({0.2: text_a, 0.5: text_b, 0.8: text_c})
    candidates = _run_diverge(coder, 3)
    winner = candidates[medoid_index(candidates)]
    assert winner in (text_a, text_b)
    assert winner != text_c


def test_failing_sample_is_dropped_round_survives() -> None:
    coder = FakeCoder({0.2: "A", 0.5: "B", 0.8: "C"}, fail_on={0.5})
    candidates = _run_diverge(coder, 3)
    assert candidates == ["A", "C"]  # B dropped, order preserved


def test_single_sample_path() -> None:
    coder = FakeCoder({0.2: "only"})
    candidates = _run_diverge(coder, 1)
    assert candidates == ["only"]


def test_detect_model_collapse() -> None:
    same = {
        CouncilRole.PLANNER: SimpleNamespace(model_id="llama3"),
        CouncilRole.CODER: SimpleNamespace(model_id="llama3"),
        CouncilRole.REVIEWER: SimpleNamespace(model_id="llama3"),
    }
    mixed = {
        CouncilRole.PLANNER: SimpleNamespace(model_id="llama3"),
        CouncilRole.CODER: SimpleNamespace(model_id="qwen-coder"),
    }
    assert detect_model_collapse(same) is True
    assert detect_model_collapse(mixed) is False
    # A single role is not "collapse" (nothing to diversify).
    assert detect_model_collapse({CouncilRole.CODER: SimpleNamespace(model_id="x")}) is False
