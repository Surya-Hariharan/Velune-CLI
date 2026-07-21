"""The REPL's content column has a fixed ceiling — it must not stretch to
fill an arbitrarily wide terminal.

A terminal emulator's own font-size zoom (Ctrl+scroll / Ctrl+Plus-Minus) never
reaches this process as input, so it can't be intercepted or blocked here —
this only fixes the *layout*: past `_MAX_CONTENT_WIDTH` (fullscreen.py), a
wider window just grows blank gutters on both sides instead of stretching the
conversation pane, borders, or banner. A narrower window still shrinks the
content normally (this is a ceiling, not a fixed size that could break a
small window).

Drives a real `Application` against a real `Vt100_Output` over `StringIO`,
mirroring `test_overlay_collapse.py`'s harness, and asserts on the rendered
prompt-box border's actual length — the simplest on-screen proxy for "how
wide did the content column actually get allocated."
"""

from __future__ import annotations

import asyncio
import io

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output.vt100 import Vt100_Output

from velune.cli.command_palette import PALETTE_STYLES, CommandPalette
from velune.cli.fullscreen import _MAX_CONTENT_WIDTH, FullscreenREPLUI
from velune.cli.inline_flow import InlineFlow
from velune.cli.statusbar import StatusBarState

_SETTLE = 0.25


def _build_ui(inp, columns: int) -> FullscreenREPLUI:
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=45, columns=columns))
    flow = InlineFlow()
    palette = CommandPalette([], suppressed=flow.is_active)
    kb = KeyBindings()
    palette.add_bindings(kb)
    flow.add_bindings(kb)
    return FullscreenREPLUI(
        status_state=StatusBarState(),
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments=dict(PALETTE_STYLES),
        key_bindings=kb,
        on_interrupt=lambda _e: False,
        command_palette=palette,
        inline_flow=flow,
        output=output,
        input=inp,
    )


def _run_and_measure(columns: int) -> tuple[int, int]:
    """(left gutter, border width) of the top prompt border on a real screen.

    Checking the border's *length alone* isn't enough to prove the container
    itself was capped — `_render_prompt_top_border` builds a string sized off
    `_width()`, so a regression that drops the layout-level cap but leaves
    `_width()` clamped would still produce a short string, just left-aligned
    inside a still-full-width Window (a blank strip on the right, not
    centered gutters on both sides). The left-gutter offset is what actually
    distinguishes "letterboxed" from "short string, wrong side padded."
    """

    async def _main():
        with create_pipe_input() as inp:
            ui = _build_ui(inp, columns)
            ui._running = True
            task = asyncio.ensure_future(ui.run())
            await asyncio.sleep(_SETTLE)
            try:
                screen = ui._app.renderer._last_screen
                for y in range(screen.height):
                    row = screen.data_buffer.get(y, {})
                    non_blank = sorted(x for x in row if row[x].char != " ")
                    if non_blank and row[non_blank[0]].char == "╭":
                        left = non_blank[0]
                        width = non_blank[-1] - left + 1
                        return left, width
                return -1, 0
            finally:
                ui.stop()
                await task

    return asyncio.run(_main())


@pytest.mark.timeout(30)
def test_content_column_is_centered_and_capped_on_a_wide_terminal():
    columns = 220
    left, width = _run_and_measure(columns)

    assert width == _MAX_CONTENT_WIDTH, (
        f"a {columns}-column terminal rendered the border at {width} cells, "
        f"expected it capped at {_MAX_CONTENT_WIDTH}"
    )
    expected_gutter = (columns - _MAX_CONTENT_WIDTH) // 2
    assert left == pytest.approx(expected_gutter, abs=2), (
        f"border started at column {left}, expected it centered with a "
        f"~{expected_gutter}-column gutter — it isn't letterboxed"
    )


@pytest.mark.timeout(30)
def test_content_column_still_fills_a_narrow_terminal():
    columns = 60
    left, width = _run_and_measure(columns)

    assert left == 0, "a terminal narrower than the cap should have no left gutter"
    assert width == columns, (
        f"a {columns}-column terminal (narrower than the {_MAX_CONTENT_WIDTH}-cell "
        f"cap) rendered the border at {width} cells instead of filling the window"
    )
