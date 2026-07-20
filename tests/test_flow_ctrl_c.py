"""Ctrl+C during /connect is a soft cancel, at every stage.

Driven as real keystrokes through a live Application, because the thing most
likely to break is not the cancel logic but whether the keypress reaches it:
`FullscreenREPLUI` binds Ctrl+C eagerly for its own interrupt handling, and two
eager bindings on one key resolve to whichever was registered last — which is
the REPL's, not the flow's. The flow is reached only because that handler defers
to it explicitly, and nothing but a real keypress proves it still does.
"""

from __future__ import annotations

import asyncio
import contextlib
import io

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output.vt100 import Vt100_Output

from velune.cli.command_palette import PALETTE_STYLES, CommandPalette
from velune.cli.fullscreen import FullscreenREPLUI
from velune.cli.inline_flow import FlowCancelled, InlineFlow
from velune.cli.interactive.widgets import Option
from velune.cli.statusbar import StatusBarState

CTRL_C = "\x03"
OPTIONS = [Option("groq", "Groq", "Fast inference"), Option("openai", "OpenAI", "GPT models")]
_SETTLE = 0.25


class _Harness:
    """The REPL's real UI, its real key bindings, and a pipe to type into."""

    def __init__(self, pipe) -> None:
        self.flow = InlineFlow()
        self.palette = CommandPalette([], suppressed=self.flow.is_active)
        kb = KeyBindings()
        self.palette.add_bindings(kb)
        self.flow.add_bindings(kb)
        # Every call the REPL's own Ctrl+C handler makes when the flow declines
        # it — this is what would start the "press again to exit" countdown.
        self.repl_interrupts: list[int] = []
        self.ui = FullscreenREPLUI(
            status_state=StatusBarState(),
            history=InMemoryHistory(),
            completer=None,
            validator=None,
            style_fragments=dict(PALETTE_STYLES),
            key_bindings=kb,
            on_interrupt=lambda _e: (self.repl_interrupts.append(1), False)[1],
            command_palette=self.palette,
            inline_flow=self.flow,
            output=Vt100_Output(io.StringIO(), lambda: Size(rows=45, columns=160)),
            input=pipe,
        )
        self.ui._history_started = True
        self.app = self.ui._app

    def height(self) -> int:
        screen = self.app.renderer._last_screen
        return screen.height if screen else 0


def _run(body):
    async def _main():
        with create_pipe_input() as pipe:
            h = _Harness(pipe)
            h.ui._running = True
            task = asyncio.ensure_future(h.app.run_async())
            await asyncio.sleep(_SETTLE)
            try:
                return await body(h, pipe)
            finally:
                h.app.exit()
                await task

    return asyncio.run(_main())


@pytest.mark.timeout(30)
def test_ctrl_c_while_picking_a_provider_cancels_the_flow():
    async def _body(h: _Harness, pipe):
        step = asyncio.ensure_future(h.flow.select("Add Provider", OPTIONS))
        await asyncio.sleep(_SETTLE)
        assert h.flow.is_active()

        pipe.send_text(CTRL_C)
        await asyncio.sleep(_SETTLE)

        raised = None
        try:
            await step
        except FlowCancelled as exc:
            raised = exc
        return raised, h.flow.is_active(), h.repl_interrupts

    raised, still_active, repl_interrupts = _run(_body)

    assert isinstance(raised, FlowCancelled)
    assert still_active is False
    # The flow consumed the press, so the REPL's exit countdown never started.
    assert repl_interrupts == []


@pytest.mark.timeout(30)
def test_ctrl_c_while_entering_a_key_discards_what_was_typed():
    async def _body(h: _Harness, pipe):
        step = asyncio.ensure_future(h.flow.prompt_text("Groq API key", password=True))
        await asyncio.sleep(_SETTLE)

        pipe.send_text("gsk-half-typed-secret")
        await asyncio.sleep(_SETTLE)
        typed = h.ui.buffer.text

        pipe.send_text(CTRL_C)
        await asyncio.sleep(_SETTLE)

        raised = None
        try:
            await step
        except FlowCancelled as exc:
            raised = exc
        return raised, typed, h.ui.buffer.text, h.flow.is_masked()

    raised, typed, leftover, masked = _run(_body)

    assert isinstance(raised, FlowCancelled)
    assert typed == "gsk-half-typed-secret", "the field should have accepted the key"
    assert leftover == "", "a half-typed API key must not be left in the prompt box"
    assert masked is False, "masking must be lifted once the step is gone"


@pytest.mark.timeout(30)
def test_ctrl_c_during_verification_stops_the_in_flight_work():
    """The stage that has no keystroke to wait on — Ctrl+C must still land."""
    completed: list[str] = []

    async def _body(h: _Harness, pipe):
        async def _slow():
            await asyncio.sleep(5)
            completed.append("ran to completion")
            return "value"

        step = asyncio.ensure_future(
            h.flow.run_status(_slow(), pending="Verifying with Groq…", ok="ok", fail="no")
        )
        await asyncio.sleep(_SETTLE)
        assert h.flow.is_active()

        pipe.send_text(CTRL_C)
        await asyncio.sleep(_SETTLE)

        raised = None
        try:
            await step
        except FlowCancelled as exc:
            raised = exc
        return raised, h.flow.is_active()

    raised, still_active = _run(_body)

    assert isinstance(raised, FlowCancelled)
    assert still_active is False
    assert completed == [], "the provider call kept running after Ctrl+C"


@pytest.mark.timeout(30)
def test_the_frame_returns_to_its_idle_height_after_a_cancel():
    """A cancelled flow must leave the terminal exactly as it found it."""

    async def _body(h: _Harness, pipe):
        idle = h.height()
        step = asyncio.ensure_future(h.flow.select("Add Provider", OPTIONS))
        await asyncio.sleep(_SETTLE)
        during = h.height()

        pipe.send_text(CTRL_C)
        await asyncio.sleep(_SETTLE)
        with contextlib.suppress(FlowCancelled):
            await step
        h.app.invalidate()
        await asyncio.sleep(_SETTLE)
        return idle, during, h.height()

    idle, during, after = _run(_body)

    assert during > idle
    assert after == idle, f"frame kept {after - idle} blank rows after Ctrl+C"


@pytest.mark.timeout(30)
def test_ctrl_c_with_no_flow_running_still_reaches_the_repl():
    """The soft cancel must not swallow Ctrl+C at the ordinary prompt.

    Otherwise the double-press exit, and interrupting a running turn, would both
    silently stop working the moment this feature shipped.
    """

    async def _body(h: _Harness, pipe):
        assert h.flow.is_active() is False
        pipe.send_text(CTRL_C)
        await asyncio.sleep(_SETTLE)
        return h.repl_interrupts

    assert _run(_body) == [1], "Ctrl+C at the idle prompt never reached the REPL"
