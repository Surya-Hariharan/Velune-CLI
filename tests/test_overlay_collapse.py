"""The frame must give back the rows a floating overlay borrowed.

`Renderer.render()` sizes a non-full-screen frame to
``max(_min_available_height, last_height, preferred)`` — it never shrinks while
one screen is being diffed against the next. So any Float tall enough to grow
the frame (the command palette and InlineFlow steps are both 18 rows) reserved
that height permanently once it closed, leaving a block of blank lines above the
status bar with the prompt box stretched to fill it.

These drive a real Application against a real Vt100 output over StringIO and
assert on the rendered screen height, because the bug lives entirely in the
renderer's sizing — nothing about the widgets or the layout tree looks wrong.
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

OPTIONS = [Option("groq", "Groq", "Fast inference"), Option("openai", "OpenAI", "GPT models")]

# Long enough for the Application to service an invalidate() and for the
# call_soon-deferred collapse to run.
_SETTLE = 0.25


class _Harness:
    def __init__(self, inp) -> None:
        output = Vt100_Output(io.StringIO(), lambda: Size(rows=45, columns=160))
        self.flow = InlineFlow()
        self.palette = CommandPalette([], suppressed=self.flow.is_active)
        kb = KeyBindings()
        self.palette.add_bindings(kb)
        self.flow.add_bindings(kb)
        self.ui = FullscreenREPLUI(
            status_state=StatusBarState(),
            history=InMemoryHistory(),
            completer=None,
            validator=None,
            style_fragments=dict(PALETTE_STYLES),
            key_bindings=kb,
            on_interrupt=lambda _e: False,
            command_palette=self.palette,
            inline_flow=self.flow,
            output=output,
            input=inp,
        )
        # Post-first-turn state: the transcript is already in real scrollback,
        # so the live frame is at its minimum and any growth is visible.
        self.ui._history_started = True
        self.app = self.ui._app

    def height(self) -> int:
        screen = self.app.renderer._last_screen
        return screen.height if screen else 0

    async def settle(self) -> None:
        self.app.invalidate()
        await asyncio.sleep(_SETTLE)


def _run(body):
    async def _main():
        with create_pipe_input() as inp:
            h = _Harness(inp)
            h.ui._running = True
            task = asyncio.ensure_future(h.app.run_async())
            await asyncio.sleep(_SETTLE)
            try:
                return await body(h)
            finally:
                h.app.exit()
                await task

    return asyncio.run(_main())


@pytest.mark.timeout(30)
def test_a_flow_step_gives_its_rows_back_when_it_closes():
    async def _body(h: _Harness):
        idle = h.height()
        step = asyncio.ensure_future(h.flow.select("Connect provider", OPTIONS))
        await h.settle()
        open_height = h.height()

        h.flow.cancel()
        with contextlib.suppress(FlowCancelled):
            await step
        await h.settle()
        return idle, open_height, h.height()

    idle, open_height, closed = _run(_body)

    assert open_height > idle, "the panel should have grown the frame"
    assert closed == idle, f"frame kept {closed - idle} blank rows after the panel closed"


@pytest.mark.timeout(30)
def test_the_panel_never_collapses_between_two_steps_of_one_flow():
    """Pick-then-paste must not blink: no collapse may run mid-flow.

    Consecutive steps close and reopen the panel without yielding to the event
    loop, so no render observes the gap — which is exactly what makes the
    falling-edge watcher safe here. If it ever did fire between steps, the user
    would see the panel vanish and reappear.
    """

    async def _body(h: _Harness):
        collapses = []
        original = h.ui.collapse
        h.ui.collapse = lambda: (collapses.append(1), original())[1]

        pick = asyncio.ensure_future(h.flow.select("Connect provider", OPTIONS))
        await h.settle()
        h.flow._resolve("groq")
        await pick

        key = asyncio.ensure_future(h.flow.prompt_text("Groq API key", password=True))
        await h.settle()
        mid_flow_collapses = len(collapses)
        during = h.height()

        h.flow.cancel()
        with contextlib.suppress(FlowCancelled):
            await key
        await h.settle()
        return mid_flow_collapses, during, len(collapses)

    mid_flow, during, total = _run(_body)

    assert mid_flow == 0, "the panel collapsed between two steps of one flow"
    assert during > 0
    assert total == 1, "the panel should collapse exactly once, at the end"


@pytest.mark.timeout(30)
def test_the_command_palette_gives_its_rows_back_too():
    """The same defect, pre-existing, on the `/` palette — one rule fixes both."""

    async def _body(h: _Harness):
        idle = h.height()
        h.ui.buffer.text = "/conn"
        await h.settle()
        open_height = h.height()

        h.palette.dismiss()
        await h.settle()
        return idle, open_height, h.height()

    idle, open_height, closed = _run(_body)

    assert open_height > idle
    assert closed == idle, f"frame kept {closed - idle} blank rows after Esc"


@pytest.mark.timeout(30)
def test_collapse_is_inert_when_the_app_is_not_running():
    """Unit tests construct this class without ever calling run()."""
    with create_pipe_input() as inp:
        h = _Harness(inp)
        assert h.ui._running is False
        h.ui.collapse()  # must not raise
