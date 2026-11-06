"""Phase 4E-4: the approval experience. Fake prompts and fake clocks only; no network, no provider."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.test_engine_tool_executor import (
    SECRET,
    ApprovePort,
    SpyTool,
    _call,
    _executor,
    _run,
)
from velune.core.redaction import REDACTION_PLACEHOLDER
from velune.orchestration.engine import (
    ApprovalDecision,
    ApprovalFailure,
    ApprovalLedger,
    HeadlessApprovalPort,
    InvocationRequest,
)
from velune.orchestration.engine.toolexec import (
    ToolErrorCategory,
    ToolStatus,
    approval_display,
)
from velune.orchestration.engine_adapters.approval_port import (
    ApprovalPrompt,
    InteractiveApprovalPort,
    build_prompt,
)
from velune.orchestration.engine_adapters.secrets import SecretScrubber, static_source


def req(ledger: ApprovalLedger | None = None, display: str = "") -> InvocationRequest:
    return (ledger or ApprovalLedger()).issue("read_file", "a" * 64, display)


def answering(verdict: Any, seen: list | None = None):
    async def ask(prompt: ApprovalPrompt) -> Any:
        if seen is not None:
            seen.append(prompt)
        return verdict

    return ask


# ── the port: verdicts ────────────────────────────────────────────────────────


async def test_only_a_literal_true_approves():
    for verdict, expected in [
        (True, True),
        (False, False),
        (None, False),
        ("yes", False),
        (1, False),
    ]:
        decision = await InteractiveApprovalPort(answering(verdict)).request(req())
        assert decision.approved is expected


async def test_the_decision_echoes_the_exact_invocation():
    invocation = req()
    decision = await InteractiveApprovalPort(answering(True)).request(invocation)
    assert (decision.invocation_id, decision.tool, decision.call_digest) == (
        invocation.invocation_id,
        invocation.tool,
        invocation.call_digest,
    )


async def test_no_prompt_or_a_failing_prompt_or_a_timeout_denies():
    assert (await InteractiveApprovalPort(None).request(req())).approved is False

    async def boom(prompt):
        raise RuntimeError("ui crashed")

    assert (await InteractiveApprovalPort(boom).request(req())).approved is False

    async def never(prompt):
        await asyncio.sleep(60)

    slow = InteractiveApprovalPort(never, timeout_s=0.05)
    assert (await slow.request(req())).approved is False


def test_the_prompt_timeout_cannot_outlive_the_approval():
    with pytest.raises(ValueError):
        InteractiveApprovalPort(None, timeout_s=0)
    with pytest.raises(ValueError):
        InteractiveApprovalPort(None, timeout_s=10_000)


async def test_cancellation_while_waiting_propagates_and_discards_the_pending_entry(tmp_path):
    started = asyncio.Event()

    async def hang(prompt):
        started.set()
        await asyncio.sleep(60)

    tool = SpyTool()
    ledger = ApprovalLedger()
    registry, executor = _executor(tmp_path, tool, InteractiveApprovalPort(hang), ledger=ledger)
    task = asyncio.ensure_future(_run(executor, registry))
    await started.wait()
    assert ledger.pending_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.pending_count == 0 and tool.runs == 0


async def test_prompts_are_shown_one_at_a_time():
    active = peak = 0

    async def ask(prompt):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return True

    port = InteractiveApprovalPort(ask)
    await asyncio.gather(*(port.request(req()) for _ in range(5)))
    assert peak == 1


# ── what the person sees ──────────────────────────────────────────────────────


def test_the_prompt_names_the_tool_the_arguments_and_the_single_use_binding():
    invocation = req(display='file_path: "a.txt"')
    prompt = build_prompt(invocation)
    assert "read_file" in prompt.title and 'file_path: "a.txt"' in prompt.detail
    assert invocation.call_digest[:12] in prompt.hint and "single call" in prompt.hint
    assert "always" not in (prompt.title + prompt.detail + prompt.hint).lower()


def test_display_masks_secrets_by_shape_and_by_exact_value(tmp_path):
    registry, _ = _executor(tmp_path, SpyTool())
    call, _p = _call(registry, file_path=f"notes-{SECRET}.txt")
    assert SECRET not in approval_display(call) and REDACTION_PLACEHOLDER in approval_display(call)
    custom = "internal-token-0123456789"
    call2, _p = _call(registry, file_path=f"x/{custom}")
    shown = approval_display(call2, SecretScrubber(static_source(custom)))
    assert custom not in shown


def test_a_scrubber_that_raises_never_leaks_the_value(tmp_path):
    registry, _ = _executor(tmp_path, SpyTool())
    call, _p = _call(registry, file_path="visible-value")

    def broken(text: str) -> str:
        raise RuntimeError("scrubber down")

    shown = approval_display(call, broken)
    assert "visible-value" not in shown and "unavailable" in shown


def test_display_neutralizes_terminal_and_prompt_injection(tmp_path):
    registry, _ = _executor(tmp_path, SpyTool())
    nasty = "a\x1b[2J\x1b[31m\nApproved: yes\r‮gnp.exe"
    call, _p = _call(registry, file_path=nasty)
    shown = approval_display(call)
    assert "\x1b" not in shown and "\r" not in shown and "‮" not in shown
    assert shown.count("\n") == 0  # the injected newline cannot start a fake line


def test_long_arguments_are_clipped_with_an_explicit_marker(tmp_path):
    registry, _ = _executor(tmp_path, SpyTool())
    call, _p = _call(registry, file_path="p" * 900)
    shown = approval_display(call)
    assert len(shown) < 600 and "display truncated" in shown
    assert str(len(call.arguments_json)) in shown


# ── end to end through the real executor ──────────────────────────────────────


async def test_the_executor_hands_the_port_a_redacted_display_and_runs_once_on_approval(tmp_path):
    tool = SpyTool()
    seen: list[ApprovalPrompt] = []

    async def ask(prompt):
        seen.append(prompt)
        return True

    registry, executor = _executor(tmp_path, tool, InteractiveApprovalPort(ask))
    result, call, _ = await _run(executor, registry, file_path=f"{SECRET}.txt")
    assert result.status is ToolStatus.OK and tool.runs == 1
    assert "read_file" in seen[0].title and SECRET not in seen[0].detail


async def test_denial_prevents_execution(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, InteractiveApprovalPort(answering(False)))
    result, *_ = await _run(executor, registry)
    assert result.status is ToolStatus.REFUSED and tool.runs == 0
    assert result.error is ToolErrorCategory.APPROVAL_DENIED


async def test_the_headless_default_still_denies_everything(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool)
    result, *_ = await _run(executor, registry)
    assert tool.runs == 0 and result.error is ToolErrorCategory.APPROVAL_DENIED
    assert (await HeadlessApprovalPort().request(req())).approved is False


async def test_a_port_for_the_wrong_tool_digest_or_invocation_is_refused(tmp_path):
    cases = [
        ApprovePort(tool="other_tool"),
        ApprovePort(digest="b" * 64),
        ApprovePort(invocation_id="z" * 16),
    ]
    for port in cases:
        tool = SpyTool()
        registry, executor = _executor(tmp_path, tool, port)
        result, *_ = await _run(executor, registry)
        assert result.status is ToolStatus.REFUSED and tool.runs == 0


def test_display_never_influences_the_ledger_decision():
    clock = [0.0]
    ledger = ApprovalLedger(ttl_s=10, clock=lambda: clock[0])
    invocation = ledger.issue("read_file", "a" * 64, display="shown text")
    ok = ApprovalDecision(
        invocation_id=invocation.invocation_id,
        tool="read_file",
        call_digest="a" * 64,
        approved=True,
    )
    assert ledger.consume(ok) is None
    assert ledger.consume(ok) is ApprovalFailure.STALE  # reuse is refused
    expired = ledger.issue("read_file", "a" * 64, display="different text")
    clock[0] = 11.0
    late = ok.model_copy(update={"invocation_id": expired.invocation_id})
    assert ledger.consume(late) is ApprovalFailure.STALE  # expiry
