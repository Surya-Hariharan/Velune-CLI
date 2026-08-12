"""The REPL's content column fills the full terminal width by default.

`max_content_width` is opt-in: passing `None` (the default) must never
letterbox the layout — the conversation pane, borders, and banner should
stretch to whatever width the terminal actually is, on both wide and narrow
terminals. Passing an explicit cap restores the old letterboxed behavior,
pinned to the left edge with the leftover width sent to a trailing gutter.

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

from velune.cli.command_palette import CommandPalette, palette_styles
from velune.cli.fullscreen import FullscreenREPLUI
from velune.cli.inline_flow import InlineFlow
from velune.cli.statusbar import StatusBarState

_SETTLE = 0.25


def _build_ui(inp, columns: int, *, max_content_width: int | None = None) -> FullscreenREPLUI:
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
        style_fragments=palette_styles(),
        key_bindings=kb,
        on_interrupt=lambda _e: False,
        command_palette=palette,
        inline_flow=flow,
        output=output,
        input=inp,
        max_content_width=max_content_width,
    )


def _run_and_measure(columns: int, *, max_content_width: int | None = None) -> tuple[int, int]:
    """(left gutter, border width) of the top prompt border on a real screen."""

    async def _main():
        with create_pipe_input() as inp:
            ui = _build_ui(inp, columns, max_content_width=max_content_width)
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
def test_content_column_fills_a_wide_terminal_by_default():
    columns = 220
    left, width = _run_and_measure(columns)

    assert left == 0, "with no configured cap, a wide terminal should have no left gutter"
    assert width == columns, (
        f"a {columns}-column terminal rendered the border at {width} cells, "
        "expected it to fill the whole width by default"
    )


@pytest.mark.timeout(30)
def test_content_column_still_fills_a_narrow_terminal():
    columns = 60
    left, width = _run_and_measure(columns)

    assert left == 0, "a narrow terminal should have no left gutter"
    assert width == columns, (
        f"a {columns}-column terminal rendered the border at {width} cells "
        "instead of filling the window"
    )


@pytest.mark.timeout(30)
def test_configured_cap_still_letterboxes_a_wide_terminal():
    columns, cap = 220, 100
    left, width = _run_and_measure(columns, max_content_width=cap)

    assert width == cap, (
        f"a {columns}-column terminal with content_max_width={cap} rendered the "
        f"border at {width} cells, expected it capped at {cap}"
    )
    assert left == 0, (
        "an explicitly configured cap is pinned to the left edge (not centered) "
        f"— expected no left gutter, got {left}"
    )
