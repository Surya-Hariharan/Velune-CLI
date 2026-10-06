"""A seat that fails must never look like a seat that answered.

Covers the failure primitive (strict ``deliberate``, abstaining typed messages)
and its consequences for judges (Issue 2), the Synthesizer and hard failures
(Issue 3), plus the two audit defects folded into the same commit: the debate
revision now sees the proposal it is revising, and the Synthesizer is told how
confident the council actually is.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.council_fakes import (
    CODE,
    REVIEW_FAIL,
    REVIEW_OK,
    FakeProvider,
    healthy,
    make_model,
    make_orchestrator,
    seat_of,
)
from velune.cognition.council.base import (
    AGENT_FAILURE_PREFIXES,
    CouncilAgentError,
    is_failure_text,
)
from velune.cognition.council.challenger import ChallengerAgent
from velune.cognition.council.critics import SecurityCritic
from velune.cognition.council.messages import ChallengerMessage, CriticMessage, ReviewerMessage
from velune.cognition.council.reviewer import ReviewerAgent
from velune.core.errors.provider import (
    InferenceError,
    ProviderAuthenticationError,
)
from velune.models.specializations import CouncilRole
from velune.orchestration.schemas import ExecutionStatus

MESSAGES = [{"role": "user", "content": "judge this"}]


def _reviewer(responder):
    provider = FakeProvider("fake", responder)
    return ReviewerAgent(make_model(), provider), provider


# ── strict deliberate ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (TimeoutError(), "timeout"),
        (InferenceError("backend exploded"), "provider"),
        ("   ", "empty"),
    ],
)
async def test_strict_deliberate_raises_instead_of_returning_a_sentinel(failure, kind):
    agent, _ = _reviewer(lambda seat, request: failure)
    with pytest.raises(CouncilAgentError) as excinfo:
        await agent.deliberate(MESSAGES, strict=True)
    assert excinfo.value.kind == kind
    assert excinfo.value.role == CouncilRole.REVIEWER.value


async def test_strict_auth_failure_is_typed_and_marks_the_key(monkeypatch):
    marked: list[str] = []
    monkeypatch.setattr(
        "velune.providers.keystore.mark_invalid", lambda pid, reason="": marked.append(pid)
    )
    agent, _ = _reviewer(lambda seat, request: ProviderAuthenticationError("bad key"))
    with pytest.raises(CouncilAgentError) as excinfo:
        await agent.deliberate(MESSAGES, strict=True)
    assert excinfo.value.kind == "auth"
    assert marked == ["fake"]


async def test_non_strict_deliberate_keeps_the_legacy_sentinels():
    agent, _ = _reviewer(lambda seat, request: TimeoutError())
    text = await agent.deliberate(MESSAGES)
    assert text.startswith("[Agent reviewer timed out")
    assert is_failure_text(text)

    agent, _ = _reviewer(lambda seat, request: InferenceError("down"))
    text = await agent.deliberate(MESSAGES)
    assert text.startswith("Deliberation failure inside agent reviewer")
    assert is_failure_text(text)


def test_is_failure_text_recognises_every_sentinel_and_blank_output():
    assert is_failure_text(None) and is_failure_text("") and is_failure_text("  \n")
    for prefix in AGENT_FAILURE_PREFIXES:
        assert is_failure_text(prefix + " something")
    assert not is_failure_text("a perfectly ordinary answer")


# ── typed messages abstain, they never approve ───────────────────────────────


@pytest.mark.parametrize("message_type", [ReviewerMessage, CriticMessage, ChallengerMessage])
def test_degraded_messages_are_unusable_and_never_pass(message_type):
    message = message_type.degraded("unavailable", "timeout: gone")
    assert message.usable is False
    assert message.status == "unavailable"
    assert message.parse_error == "timeout: gone"
    assert getattr(message, "passed", False) is False
    assert getattr(message, "severity_rating", 0.0) == 0.0


def test_ok_messages_are_usable():
    assert ReviewerMessage(passed=True).usable
    assert ReviewerMessage.model_validate_json(REVIEW_OK).status == "ok"


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (TimeoutError(), "unavailable"),
        (InferenceError("backend exploded"), "unavailable"),
        ("not json at all", "unparseable"),
        ('{"passed": "perhaps", "critical_issues": 7}', "unparseable"),
        ("", "unavailable"),
    ],
)
async def test_typed_deliberate_returns_an_abstaining_message_on_failure(raw, status):
    agent, _ = _reviewer(lambda seat, request: raw)
    result = await agent.review(task="t", proposal="p", context="c")
    assert result.status == status
    assert result.usable is False
    assert result.passed is False  # never the old default-valued "pass"


async def test_typed_deliberate_parses_valid_output_and_fenced_json():
    agent, _ = _reviewer(lambda seat, request: "```json\n" + REVIEW_OK + "\n```")
    result = await agent.review(task="t", proposal="p", context="c")
    assert result.usable and result.passed


async def test_challenger_and_critic_abstain_without_fake_findings():
    provider = FakeProvider("fake", lambda seat, request: InferenceError("down"))
    challenge = await ChallengerAgent(make_model(), provider).challenge("t", "p", "c")
    assert challenge.usable is False
    assert challenge.failure_vectors == []  # no synthetic "unparseable" finding

    critique = await SecurityCritic(make_model(), provider).critique("t", "p", "c")
    assert critique.usable is False and critique.passed is False


# ── orchestrator: failed judges are not approvals ────────────────────────────


@pytest.mark.parametrize("failure", [RuntimeError("provider down"), TimeoutError()])
async def test_reviewer_failure_in_standard_flags_no_review(monkeypatch, failure):
    def responder(seat, request):
        return failure if seat == "reviewer" else healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    arbitration = result["arbitration"]
    assert "JUDGE_UNAVAILABLE:reviewer" in arbitration["flags"]
    assert "NO_REVIEW" in arbitration["flags"]
    assert arbitration["requires_human_review"] is True
    assert arbitration["overall_confidence"] <= 0.30
    assert result["degraded"] is True
    assert "judge_unavailable:reviewer" in result["degradation_reasons"]
    assert result["reviewer_report"].status == "unavailable"


async def test_one_failed_critic_caps_confidence_but_not_the_others(monkeypatch):
    def responder(seat, request):
        return RuntimeError("down") if seat == "security" else healthy(seat, request)

    orch, provider = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    arbitration = result["arbitration"]
    assert "JUDGE_UNAVAILABLE:security" in arbitration["flags"]
    assert "NO_REVIEW" not in arbitration["flags"]
    assert arbitration["overall_confidence"] <= 0.60
    assert arbitration["requires_human_review"] is False
    for seat in ("reviewer", "challenger", "scalability", "performance", "maintainability"):
        assert seat in provider.seats_called()
    assert result["security_report"].usable is False
    assert result["reviewer_report"].usable is True


async def test_half_of_the_judges_failing_requires_human_review(monkeypatch):
    down = {"security", "performance", "maintainability"}

    def responder(seat, request):
        return RuntimeError("down") if seat in down else healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    arbitration = result["arbitration"]
    assert arbitration["requires_human_review"] is True
    assert arbitration["overall_confidence"] <= 0.40


async def test_every_judge_failing_is_reported_as_no_review(monkeypatch):
    judges = {"reviewer", "challenger", "scalability", "security", "performance", "maintainability"}

    def responder(seat, request):
        return RuntimeError("down") if seat in judges else healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    assert "NO_REVIEW" in result["arbitration"]["flags"]
    assert result["arbitration"]["overall_confidence"] <= 0.30


async def test_a_judge_that_returns_prose_is_unparseable_not_approving(monkeypatch):
    def responder(seat, request):
        return "Looks great to me!" if seat == "reviewer" else healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert result["reviewer_report"].status == "unparseable"
    assert "JUDGE_UNAVAILABLE:reviewer" in result["arbitration"]["flags"]


async def test_healthy_runs_are_unchanged(monkeypatch):
    for tier, confidence in (("standard", 0.928), ("full", 0.913)):
        orch, _ = make_orchestrator(monkeypatch)
        result = await orch.execute_task("explain it", "ctx", council_tier=tier)
        assert result["arbitration"]["overall_confidence"] == confidence
        assert result["arbitration"]["flags"] == []
        assert result["degraded"] is False
        assert result["degradation_reasons"] == []


# ── orchestrator: the debate loop ────────────────────────────────────────────


async def test_debate_revision_is_shown_the_proposal_it_revises(monkeypatch):
    seen: list[str] = []
    counts = {"coder": 0, "reviewer": 0}

    def responder(seat, request):
        if seat == "coder":
            counts["coder"] += 1
            seen.append(request.messages[-1]["content"])
            return CODE if counts["coder"] == 1 else "```python\nprint('v2')\n```"
        if seat == "reviewer":
            counts["reviewer"] += 1
            return REVIEW_FAIL if counts["reviewer"] == 1 else REVIEW_OK
        return healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert len(seen) == 2
    assert "YOUR PREVIOUS PROPOSAL:" in seen[1] and "print('hello')" in seen[1]
    assert "missing check" in seen[1]
    assert "print('v2')" in result["coder_proposal"]


async def test_a_judge_that_cannot_re_review_does_not_confirm_the_revision(monkeypatch):
    counts = {"security": 0}
    failing = json.dumps({"passed": False, "issues": ["injection"], "score": 0.2, "rationale": "x"})

    def responder(seat, request):
        if seat == "security":
            counts["security"] += 1
            return failing if counts["security"] == 1 else RuntimeError("down")
        return healthy(seat, request)

    orch, provider = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    flags = result["arbitration"]["flags"]
    assert "JUDGE_UNAVAILABLE:security(re-review)" in flags
    assert result["arbitration"]["requires_human_review"] is True
    # 3 diverge samples + exactly one revision: a judge that cannot confirm ends the debate.
    assert provider.seats_called().count("coder") == 4
    assert result["security_report"].passed is False  # the unresolved objection stays on record


async def test_failed_revision_ends_the_debate_and_keeps_the_proposal(monkeypatch):
    counts = {"coder": 0, "reviewer": 0}

    def responder(seat, request):
        if seat == "coder":
            counts["coder"] += 1
            return CODE if counts["coder"] == 1 else TimeoutError()
        if seat == "reviewer":
            counts["reviewer"] += 1
            return REVIEW_FAIL
        return healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert result["coder_proposal"] == CODE
    assert counts["reviewer"] == 1  # nobody re-reviewed failure text


async def test_failed_coder_samples_are_dropped_not_voted_on(monkeypatch):
    counts = {"coder": 0}

    def responder(seat, request):
        if seat == "coder":
            counts["coder"] += 1
            return CODE if counts["coder"] == 2 else RuntimeError("down")
        return healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    assert result["coder_proposal"] == CODE


async def test_all_coder_samples_failing_is_a_hard_failure(monkeypatch):
    def responder(seat, request):
        return RuntimeError("down") if seat == "coder" else healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="full")
    assert result["is_timeout"] is True
    assert "ALL_AGENTS_FAILED" in result["arbitration"]["flags"]


# ── synthesizer failure and arbitration signals ──────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        RuntimeError("down"),
        "   ",
        "[Agent synthesizer timed out — using empty response]",
        "Deliberation failure inside agent synthesizer: 401",
    ],
)
async def test_synthesizer_failure_is_degraded_never_the_answer(monkeypatch, failure):
    def responder(seat, request):
        return failure if seat == "synthesizer" else healthy(seat, request)

    orch, _ = make_orchestrator(monkeypatch, responder)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    summary = result["final_summary"]
    assert summary.startswith("# Council Deliberation Report (Degraded Mode)")
    assert not is_failure_text(summary)
    assert result["degraded"] is True
    assert any(r.startswith("synthesizer_unavailable") for r in result["degradation_reasons"])


async def test_synthesizer_is_told_how_confident_the_council_is(monkeypatch):
    def responder(seat, request):
        return RuntimeError("down") if seat == "security" else healthy(seat, request)

    orch, provider = make_orchestrator(monkeypatch, responder)
    await orch.execute_task("explain it", "ctx", council_tier="full")
    request = next(r for r in provider.requests if seat_of(r) == "synthesizer")
    content = request.messages[-1]["content"]
    assert "ARBITRATION SIGNALS" in content
    assert "Overall council confidence:" in content
    assert "JUDGE_UNAVAILABLE:security" in content


async def test_instant_and_minimal_results_are_not_degraded(monkeypatch):
    for tier in ("instant", "minimal"):
        orch, _ = make_orchestrator(monkeypatch)
        result = await orch.execute_task("explain it", "ctx", council_tier=tier)
        assert result["degraded"] is False and result["degradation_reasons"] == []


# ── stream(): run status ─────────────────────────────────────────────────────


async def _run_stream(orch, monkeypatch, result):
    async def fake_execute_task(*args, **kwargs):
        return result

    monkeypatch.setattr(orch, "execute_task", fake_execute_task)
    run_id = None
    async for milestone in orch.stream("task"):
        run_id = milestone.run_id
    return orch.get_state(run_id)


async def test_hard_failure_keeps_the_reason_and_is_failed(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    state = await _run_stream(orch, monkeypatch, orch._build_timeout_result("task"))
    assert state.status == ExecutionStatus.FAILED
    assert state.error and "timed out" in state.error


async def test_degraded_result_is_completed_and_carries_its_reasons(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    state = await _run_stream(
        orch,
        monkeypatch,
        {
            "final_summary": "answer",
            "task_plan": None,
            "coder_proposal": None,
            "degraded": True,
            "degradation_reasons": ["judge_unavailable:reviewer"],
        },
    )
    assert state.status == ExecutionStatus.COMPLETED
    assert state.validation_issues == ["judge_unavailable:reviewer"]


# ── REPL handler ─────────────────────────────────────────────────────────────


async def test_repl_labels_a_degraded_answer_but_still_stores_it():
    from tests.test_council_phase0_characterization import _repl, _state, _StubOrchestrator
    from velune.cli.handlers.council import execute_council_task

    state = _state()
    state.validation_issues = ["synthesizer_unavailable: timeout"]
    repl = _repl(_StubOrchestrator(state))
    await execute_council_task(repl, "do it", force_tier=None)
    out = repl.output.getvalue()
    assert "Council Result (degraded)" in out
    assert "synthesizer_unavailable" in out
    assert repl._conversation[-1]["content"] == "the answer"


async def test_repl_shows_a_failed_run_as_a_failure_and_applies_no_edits(monkeypatch):
    from tests.test_council_phase0_characterization import _repl, _state, _StubOrchestrator
    from velune.cli.handlers import council as council_handlers

    applied = MagicMock()
    monkeypatch.setattr(council_handlers, "apply_council_edits", applied)
    failed = _state(ExecutionStatus.FAILED, output="Execution failed: boom", error="boom")
    failed.coder_proposal = "```edit```"
    repl = _repl(_StubOrchestrator(failed))
    await council_handlers.execute_council_task(repl, "do it", force_tier=None)
    out = repl.output.getvalue()
    assert "Council Failed" in out and "boom" in out
    assert "Council Result" not in out
    assert repl._conversation == []
    applied.assert_not_called()


# ── ask ──────────────────────────────────────────────────────────────────────


async def test_ask_json_reports_degraded_and_abstaining_judges(monkeypatch, tmp_path, capsys):
    from velune.cli.commands import ask as ask_mod

    monkeypatch.setattr("velune.providers.keystore.list_invalid_providers", lambda: [])
    monkeypatch.setattr("velune.providers.keystore.is_ollama_live", lambda timeout=0.25: True)

    class _Lifecycle:
        async def startup(self):
            pass

        async def shutdown(self):
            pass

    class _Registry:
        async def refresh(self):
            pass

        def list_all(self):
            return [SimpleNamespace(provider_id="ollama", model_id="m")]

    class _Orchestrator:
        mapper = SimpleNamespace(map_roles=lambda self=None: {})

        async def execute_task(self, prompt, repo_context, council_tier=None):
            return {
                "tier": "standard",
                "arbitration": {"flags": ["JUDGE_UNAVAILABLE:reviewer"]},
                "final_summary": "degraded answer",
                "reviewer_report": ReviewerMessage.degraded("unavailable", "timeout: gone"),
                "challenger_report": None,
                "is_timeout": False,
                "degraded": True,
                "degradation_reasons": ["judge_unavailable:reviewer"],
            }

    services = {
        "runtime.lifecycle": _Lifecycle(),
        "runtime.model_registry": _Registry(),
        "runtime.council_orchestrator": _Orchestrator(),
        "runtime.repository_cognition": SimpleNamespace(index=lambda: None),
        "runtime.workspace": tmp_path,
    }
    container = SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services)
    ctx = SimpleNamespace(container=container, json_mode=True, workspace=tmp_path)
    await ask_mod._ask_with_runtime(ctx, "hello")
    payload = json.loads(capsys.readouterr().out)
    assert payload["degraded"] is True
    assert payload["degradation_reasons"] == ["judge_unavailable:reviewer"]
    assert payload["reviewer_report"]["status"] == "unavailable"


def test_council_view_does_not_render_an_abstaining_judge_as_a_pass():
    import io

    from rich.console import Console

    from velune.cli.display.council_view import CouncilDisplayView

    buffer = io.StringIO()
    view = CouncilDisplayView(Console(file=buffer, width=100, color_system=None))
    view.render_reviewer_report(ReviewerMessage.degraded("unavailable", "timeout"))
    view.render_challenger_report(ChallengerMessage.degraded("unparseable", "bad json"))
    out = buffer.getvalue()
    assert "PASS" not in out
    assert "no verdict" in out and "not counted as an approval" in out
