"""File-change approval must never block the event loop and must fail closed."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from velune.cli import interactive
from velune.execution import approval
from velune.execution.diff_preview import DiffDecision
from velune.execution.multi_diff import MultiDiffPreview

CHOICES = [("accept", "Accept"), ("reject", "Reject")]


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive_tty", lambda: True)


def _answer(monkeypatch, value):
    async def fake(title, options, **kw):
        return value

    monkeypatch.setattr(interactive, "single_select", fake)


async def test_no_terminal_means_reject(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive_tty", lambda: False)
    assert await approval.ask_choice("t", CHOICES, reject="reject") == "reject"


async def test_explicit_choice_is_returned(tty, monkeypatch):
    _answer(monkeypatch, "accept")
    assert await approval.ask_choice("t", CHOICES, reject="reject") == "accept"


@pytest.mark.parametrize("value", [interactive.CANCEL, interactive.BACK, None, "bogus"])
async def test_cancel_back_and_unknown_answers_reject(tty, monkeypatch, value):
    _answer(monkeypatch, value)
    assert await approval.ask_choice("t", CHOICES, reject="reject") == "reject"


async def test_widget_failure_rejects(tty, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("terminal exploded")

    monkeypatch.setattr(interactive, "single_select", boom)
    assert await approval.ask_choice("t", CHOICES, reject="reject") == "reject"


async def test_timeout_rejects_instead_of_hanging(tty, monkeypatch):
    async def never(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(interactive, "single_select", never)
    got = await approval.ask_choice("t", CHOICES, reject="reject", timeout=0.05)
    assert got == "reject"


async def test_loop_stays_responsive_while_waiting_for_an_answer(tty, monkeypatch):
    release = asyncio.Event()

    async def slow(*a, **k):
        await release.wait()
        return "accept"

    monkeypatch.setattr(interactive, "single_select", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    waiting = asyncio.create_task(approval.ask_choice("t", CHOICES, reject="reject"))
    await asyncio.sleep(0.1)
    assert ticks > 5  # a blocking input() would have frozen the loop at ~1
    release.set()
    assert await waiting == "accept"
    task.cancel()


async def test_cancellation_propagates(tty, monkeypatch):
    async def never(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(interactive, "single_select", never)
    task = asyncio.create_task(approval.ask_choice("t", CHOICES, reject="reject"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_labels_have_no_rich_markup_to_swallow():
    """Regression: '[a]ccept / [r]eject' rendered as 'ccept / eject' (Rich ate the tags)."""
    import inspect

    from velune.execution import diff_preview, hunk_review, multi_diff

    for module in (diff_preview, hunk_review, multi_diff):
        src = inspect.getsource(module)
        assert "[a]ccept" not in src and "[r]eject" not in src, module.__name__
        assert "Prompt.ask" not in src, module.__name__


async def test_batch_preview_applies_per_file_choices(tty, monkeypatch, tmp_path):
    answers = iter(["accept", "reject", "all", "accept"])

    async def fake(title, options, **kw):
        return next(answers)

    monkeypatch.setattr(interactive, "single_select", fake)
    files = {tmp_path / f"f{i}.txt": f"new {i}\n" for i in range(4)}
    out = io.StringIO()
    decisions = await MultiDiffPreview(Console(file=out, width=120)).preview_batch(files)
    got = [decisions[p] for p in files]
    # f0 accept, f1 reject, f2 "all" -> accepted, f3 auto-accepted without asking
    assert got == [
        DiffDecision.ACCEPT,
        DiffDecision.REJECT,
        DiffDecision.ACCEPT,
        DiffDecision.ACCEPT,
    ]
    assert next(answers) == "accept"  # the 4th answer was never consumed


async def test_batch_preview_without_a_terminal_rejects_everything(monkeypatch, tmp_path):
    monkeypatch.setattr(interactive, "is_interactive_tty", lambda: False)
    files: dict[Path, str] = {tmp_path / "a.txt": "x\n"}
    out = io.StringIO()
    decisions = await MultiDiffPreview(Console(file=out, width=120)).preview_batch(files)
    assert decisions == {tmp_path / "a.txt": DiffDecision.REJECT}
