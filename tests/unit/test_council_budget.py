"""Council execution budget guard tests.

Covers three guarantees:
1. Wall-time timeout surfaces ``is_timeout=True`` in the result.
2. Debate cycle cap is respected — budget.max_review_cycles=2 stops debate at 2 turns,
   not the 3 that calculate_max_debate_turns would otherwise return.
3. Event history never grows beyond 1000 entries; oldest events are silently dropped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.cognition.budget import CouncilExecutionBudget
from velune.events import _HISTORY_MAXLEN, CognitiveBus, Event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator():
    """Build a CouncilOrchestrator with all heavy dependencies stubbed."""
    from velune.cognition.orchestrator import CouncilOrchestrator

    orch = CouncilOrchestrator.__new__(CouncilOrchestrator)
    orch.config = None
    orch.analytics = MagicMock()
    orch.analytics.record_debate_outcome = MagicMock()
    orch.lineage_memory = None

    # Tier classifier always returns FULL so debate logic runs
    tier_classifier = MagicMock()
    from velune.cognition.council.tiers import CouncilTier

    tier_classifier.classify.return_value = CouncilTier.FULL
    tier_classifier.default_tier_override = None
    orch.tier_classifier = tier_classifier

    # Role mapper — minimal stub
    mapper = MagicMock()
    mapper.map_roles.return_value = {}
    mapper.profiler = MagicMock()
    mapper.profiler.get_profile.return_value = None
    orch.mapper = mapper

    orch.arbitrator = MagicMock()
    orch.arbitrator.arbitrate.return_value = MagicMock(
        to_dict=lambda: {"overall_confidence": 0.9, "requires_human_review": False}
    )
    orch.architecture_agent = MagicMock()
    orch.architecture_agent.audit_architecture.return_value = {}
    orch.architecture_agent.ledger = MagicMock()
    orch.architecture_agent.ledger.get_items.return_value = []

    # Style profile
    orch._style_cache: dict = {}  # type: ignore[attr-defined]
    orch._style_cache_time: dict = {}  # type: ignore[attr-defined]

    style_resolver = MagicMock()
    style_resolver.get_or_refresh_style_profile = AsyncMock(return_value=None)
    orch.style_resolver = style_resolver

    return orch


def _make_agent_factory(coder_proposal: str = "def foo(): pass"):
    """Return a mock AgentFactory whose agents yield controllable responses."""
    factory = MagicMock()

    # Planner
    planner = MagicMock()
    plan = MagicMock()
    plan.steps = []
    planner.generate_plan = AsyncMock(return_value=plan)

    # Coder — always returns the fixed proposal immediately
    coder = MagicMock()
    coder.write_code = AsyncMock(return_value=coder_proposal)

    # Reviewer — always rejects
    reviewer = MagicMock()
    from velune.cognition.council.messages import ReviewerMessage

    reviewer.review = AsyncMock(
        return_value=ReviewerMessage(
            passed=False,
            confidence_rating=0.2,
            critical_issues=["always reject"],
        )
    )

    # Critics — security passes, scalability fails (so calculate_max_debate_turns returns >= 3)
    from velune.cognition.council.messages import ChallengerMessage, CriticMessage

    challenger = MagicMock()
    challenger.challenge = AsyncMock(
        return_value=ChallengerMessage(
            assumptions_challenged=[],
            failure_vectors=["scalability concern"],
            severity_rating=0.9,  # high severity → +1 turn from calculate_max_debate_turns
        )
    )

    scalability_critic = MagicMock()
    scalability_critic.critique = AsyncMock(
        return_value=CriticMessage(passed=False, issues=["O(n^2) loop"], score=0.4, rationale="")
    )

    security_critic = MagicMock()
    security_critic.critique = AsyncMock(
        return_value=CriticMessage(passed=True, issues=[], score=1.0, rationale="")
    )

    performance_critic = MagicMock()
    performance_critic.critique = AsyncMock(
        return_value=CriticMessage(passed=True, issues=[], score=1.0, rationale="")
    )

    maintainability_critic = MagicMock()
    maintainability_critic.critique = AsyncMock(
        return_value=CriticMessage(passed=True, issues=[], score=1.0, rationale="")
    )

    synthesizer = MagicMock()
    synthesizer.synthesize = AsyncMock(return_value="synthesis")

    factory.create_coder.return_value = coder
    factory.create_planner.return_value = planner
    factory.create_reviewer.return_value = reviewer
    factory.create_synthesizer.return_value = synthesizer
    factory.create_challenger.return_value = challenger
    factory.create_scalability_critic.return_value = scalability_critic
    factory.create_security_critic.return_value = security_critic
    factory.create_performance_critic.return_value = performance_critic
    factory.create_maintainability_critic.return_value = maintainability_critic

    return factory


# ---------------------------------------------------------------------------
# Test 1: Wall-time timeout → is_timeout=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.critical
async def test_execute_task_returns_is_timeout_on_wall_time_exceeded():
    """When the wall-time budget is exceeded, execute_task must return is_timeout=True."""
    orch = _make_orchestrator()

    # Agent factory whose coder sleeps longer than the budget
    factory = MagicMock()
    coder = MagicMock()

    async def _slow_write_code(**_kwargs):
        await asyncio.sleep(10)
        return "never reached"

    coder.write_code = _slow_write_code
    planner = MagicMock()
    plan = MagicMock()
    plan.steps = []
    planner.generate_plan = AsyncMock(return_value=plan)
    factory.create_coder.return_value = coder
    factory.create_planner.return_value = planner
    factory.create_reviewer.return_value = MagicMock()
    factory.create_synthesizer.return_value = MagicMock()
    factory.create_challenger.return_value = MagicMock()
    factory.create_scalability_critic.return_value = MagicMock()
    factory.create_security_critic.return_value = MagicMock()
    factory.create_performance_critic.return_value = MagicMock()
    factory.create_maintainability_critic.return_value = MagicMock()
    orch.agent_factory = factory

    budget = CouncilExecutionBudget(
        max_wall_time_seconds=1,
        coder_timeout_seconds=1,
        planner_timeout_seconds=1,
        max_review_cycles=2,
    )

    with (
        patch("velune.cognition.firewall.CognitiveFirewall") as _fw,
        patch("velune.core.trace.TraceContext"),
    ):
        _fw.return_value.scan_file_for_injection.return_value = {"is_safe": True}
        _fw.return_value.wrap_workspace_content.side_effect = lambda _tag, text: text
        result = await orch.execute_task(
            prompt="add a sort function",
            repo_context="# empty",
            council_tier="full",
            budget=budget,
        )

    assert result.get("is_timeout") is True, f"Expected is_timeout=True, got: {result}"


# ---------------------------------------------------------------------------
# Test 2: Debate cycle cap — budget.max_review_cycles=2 wins over calculate_max_debate_turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.critical
async def test_debate_stops_at_budget_max_review_cycles():
    """With max_review_cycles=2, the debate loop must stop after exactly 2 turns
    even when calculate_max_debate_turns would return 3 or more."""
    orch = _make_orchestrator()
    factory = _make_agent_factory()
    orch.agent_factory = factory

    budget = CouncilExecutionBudget(
        max_wall_time_seconds=30,
        coder_timeout_seconds=10,
        planner_timeout_seconds=10,
        max_review_cycles=2,
    )

    with (
        patch("velune.cognition.firewall.CognitiveFirewall") as _fw,
        patch("velune.core.trace.TraceContext"),
        patch("velune.cognition.orchestrator.calculate_max_debate_turns", return_value=4) as _calc,
    ):
        _fw.return_value.scan_file_for_injection.return_value = {"is_safe": True}
        _fw.return_value.wrap_workspace_content.side_effect = lambda _tag, text: text
        await orch.execute_task(
            prompt="refactor the auth module",
            repo_context="# repo context",
            council_tier="full",
            budget=budget,
        )

    # calculate_max_debate_turns returned 4, but budget caps at 2.
    # coder.write_code is called: 1 initial + up to 2 refinements = at most 3 total.
    coder = factory.create_coder.return_value
    total_coder_calls = coder.write_code.call_count
    # The initial call + at most max_review_cycles refinements
    assert total_coder_calls <= 1 + budget.max_review_cycles, (
        f"Expected at most {1 + budget.max_review_cycles} coder calls, got {total_coder_calls}. "
        "Debate cycle cap is not being respected."
    )
    # Must have been called at least once (initial proposal)
    assert total_coder_calls >= 1


# ---------------------------------------------------------------------------
# Test 3: Event history capped at _HISTORY_MAXLEN after excess emits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.critical
async def test_event_history_capped_at_max_len():
    """Emitting more than _HISTORY_MAXLEN events must not grow _history beyond the cap."""
    bus = CognitiveBus()

    overflow_count = _HISTORY_MAXLEN + 1000

    for i in range(overflow_count):
        await bus.emit(Event(event_type="test.tick", source="test", data={"i": i}))

    assert len(bus._history) == _HISTORY_MAXLEN, (
        f"Expected history length {_HISTORY_MAXLEN}, got {len(bus._history)}. "
        "Rolling-window cap is not enforced."
    )
    # The oldest event should be the one emitted at index (overflow_count - _HISTORY_MAXLEN)
    expected_oldest_i = overflow_count - _HISTORY_MAXLEN
    actual_oldest_i = bus._history[0].data["i"]
    assert actual_oldest_i == expected_oldest_i, (
        f"Expected oldest event index {expected_oldest_i}, got {actual_oldest_i}. "
        "Old events are not being dropped from the front."
    )
