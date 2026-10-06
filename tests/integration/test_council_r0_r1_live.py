"""Opt-in live smoke for the deliberative engine's R0 + R1 (real provider, real calls).

Skipped unless ``VELUNE_LIVE_TESTS=1``; it spends a handful of cheap model calls against whichever
provider is configured. It asserts structure only (valid contracts, distinct seats, no answer,
nothing a peer wrote in a request is knowable from here), never wording. Visibility is proven
offline against the exact provider requests in ``test_council_r1_independence.py``.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("VELUNE_LIVE_TESTS") != "1",
        reason="live provider test; set VELUNE_LIVE_TESTS=1 to run",
    ),
]

QUESTION = "Should a two-person startup start with a monolith or with microservices?"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", text.lower()))


async def test_live_explore_produces_typed_independent_perspectives(tmp_path, monkeypatch):
    monkeypatch.setenv("VELUNE_COGNITION__COUNCIL_ENGINE", "deliberative")

    from velune.cognition.execution_trace import trace_request
    from velune.core.runtime import build_runtime
    from velune.council.adapters.engine import DeliberativeEngine
    from velune.council.profiles import GENERAL_PROFILE
    from velune.council.report import frame_of, perspectives_of
    from velune.council.results import OutcomeStatus

    runtime = build_runtime(tmp_path)
    container = runtime.container
    lifecycle = container.get("runtime.lifecycle")
    await lifecycle.startup()
    try:
        await container.get("runtime.model_registry").refresh()
        engine = DeliberativeEngine.create(container, runtime.config)
        with trace_request("live-smoke") as trace:
            outcome = await engine.explore(QUESTION)

        request = engine.build_request(QUESTION)
        assert outcome.answer is None
        assert outcome.status in (OutcomeStatus.COMPLETED, OutcomeStatus.DEGRADED), (
            outcome.failure_summary
        )
        frame = frame_of(outcome, request)
        perspectives = perspectives_of(outcome)
        assert frame is not None and len(perspectives) >= 3
        assert {"skeptic", "fact_checker"} & set(perspectives)

        ids = [claim.id for p in perspectives.values() for claim in p.claims]
        assert len(ids) == len(set(ids))  # core-assigned, collision-free
        for seat, perspective in perspectives.items():
            assert perspective.seat_id == seat
            prefix = GENERAL_PROFILE.seat(seat).claim_prefix
            assert all(claim.id.startswith(prefix + "-") for claim in perspective.claims)

        # informal, never asserted tightly: how different are the five positions?
        positions = {seat: _tokens(p.position) for seat, p in perspectives.items()}
        pairs = [(a, b) for a in positions for b in positions if a < b]
        overlap = [
            len(positions[a] & positions[b]) / max(1, len(positions[a] | positions[b]))
            for a, b in pairs
        ]
        print(
            "\nlive R0/R1:",
            outcome.status.value,
            sorted(perspectives),
            "mean position overlap %.2f" % (sum(overlap) / max(1, len(overlap))),
            "calls",
            len(trace.calls),
        )
        assert trace.unexplained_calls() == []
    finally:
        await lifecycle.shutdown()
