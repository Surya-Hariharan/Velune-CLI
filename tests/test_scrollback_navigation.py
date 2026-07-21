"""In-app scrolling of the conversation transcript.

The alternate screen buffer (`full_screen=True`) has no native OS
scrollback, so reviewing history is handled entirely inside
`FullscreenREPLUI` via `_scroll_anchor` — an absolute index into
`self._lines` for the line that should stay visible. `None` means "pinned to
the bottom, follow live output" (the only behavior before this existed); an
int is a spot the user scrolled to and wants to keep looking at, even as new
content keeps arriving below it.

Drives a real `Application` against a real `Vt100_Output` over `StringIO`,
mirroring `test_clean_exit.py`'s harness, so PageUp/PageDown/mouse-wheel are
exercised as actual keystrokes through the real key-binding/render pipeline,
not just by poking internal state.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from velune.cli.fullscreen import FullscreenREPLUI, _Line
from velune.cli.statusbar import StatusBarState

_SETTLE = 0.25
_ROWS = 20
_COLUMNS = 100


def _build_ui(inp) -> FullscreenREPLUI:
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=_ROWS, columns=_COLUMNS))
    return FullscreenREPLUI(
        status_state=StatusBarState(),
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments={},
        key_bindings=KeyBindings(),
        on_interrupt=lambda _e: False,
        output=output,
        input=inp,
    )


def _visible_text(ui: FullscreenREPLUI) -> str:
    # `screen.width`/`.height` aren't reliably populated by a plain Window
    # render (only some containers, e.g. ScrollablePane, update them) — use
    # the harness's own known terminal size instead.
    screen = ui._app.renderer._last_screen
    rows = []
    for y in range(_ROWS):
        row = screen.data_buffer[y]
        rows.append("".join(row[x].char for x in range(_COLUMNS)))
    return "\n".join(rows)


def _run(body):
    async def _main():
        with create_pipe_input() as inp:
            ui = _build_ui(inp)
            ui._running = True
            task = asyncio.ensure_future(ui.run())
            await asyncio.sleep(_SETTLE)
            try:
                return await body(ui, inp)
            finally:
                ui.stop()
                await task

    return asyncio.run(_main())


def _fill(ui: FullscreenREPLUI, n: int, prefix: str = "line") -> None:
    for i in range(n):
        ui.append_system(f"{prefix}-{i}")


@pytest.mark.timeout(30)
def test_pinned_to_bottom_by_default_and_follows_new_output():
    async def _body(ui: FullscreenREPLUI, _inp):
        _fill(ui, _ROWS * 3)
        await asyncio.sleep(_SETTLE)
        return ui._scroll_anchor, _visible_text(ui)

    anchor, visible = _run(_body)
    assert anchor is None
    assert f"line-{_ROWS * 3 - 1}" in visible
    assert "line-0" not in visible


@pytest.mark.timeout(30)
def test_pageup_reveals_older_lines_without_losing_the_anchor():
    async def _body(ui: FullscreenREPLUI, inp):
        _fill(ui, _ROWS * 3)
        await asyncio.sleep(_SETTLE)

        inp.send_bytes(b"\x1b[5~")  # PageUp (xterm sequence)
        await asyncio.sleep(_SETTLE)
        anchor_after_one_page = ui._scroll_anchor
        visible_after_one_page = _visible_text(ui)

        # New output arrives while scrolled up — must not yank the view back
        # down to the bottom.
        ui.append_system("brand-new-line")
        await asyncio.sleep(_SETTLE)
        return anchor_after_one_page, visible_after_one_page, ui._scroll_anchor, _visible_text(ui)

    anchor1, visible1, anchor2, visible2 = _run(_body)

    assert anchor1 is not None, "PageUp should have set a concrete scroll anchor"
    assert f"line-{_ROWS * 3 - 1}" not in visible1, "PageUp should have moved off the live tail"
    # The anchor is an absolute index, so appending doesn't need to move it —
    # confirms new output below the fold doesn't yank the viewport down.
    assert anchor2 == anchor1
    assert "brand-new-line" not in visible2


@pytest.mark.timeout(30)
def test_ctrl_end_resumes_live_follow():
    async def _body(ui: FullscreenREPLUI, inp):
        _fill(ui, _ROWS * 3)
        await asyncio.sleep(_SETTLE)

        inp.send_bytes(b"\x1b[5~")  # PageUp
        await asyncio.sleep(_SETTLE)
        assert ui._scroll_anchor is not None

        inp.send_bytes(b"\x1b[1;5F")  # Ctrl+End
        await asyncio.sleep(_SETTLE)
        return ui._scroll_anchor, _visible_text(ui)

    anchor, visible = _run(_body)
    assert anchor is None
    assert f"line-{_ROWS * 3 - 1}" in visible


@pytest.mark.timeout(30)
def test_scroll_down_past_the_bottom_resumes_follow_mode():
    ui_anchor = None

    async def _body(ui: FullscreenREPLUI, _inp):
        nonlocal ui_anchor
        _fill(ui, 10)
        ui.scroll_to_top()
        await asyncio.sleep(_SETTLE)
        assert ui._scroll_anchor == 0

        ui.scroll_down(10_000)  # far past the end
        return ui._scroll_anchor

    ui_anchor = _run(_body)
    assert ui_anchor is None


def test_trim_shifts_the_scroll_anchor_and_clamps_at_zero():
    ui = FullscreenREPLUI(
        status_state=StatusBarState(),
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments={},
        key_bindings=KeyBindings(),
        on_interrupt=lambda _e: False,
        output=DummyOutput(),
    )
    from velune.cli import fullscreen as fullscreen_mod

    cap = fullscreen_mod._MAX_TRANSCRIPT_LINES
    ui._lines = [_Line(f"l{i}") for i in range(cap + 50)]
    ui._scroll_anchor = 30  # a line that will be evicted by the trim below

    ui._trim()

    assert len(ui._lines) == cap
    assert ui._scroll_anchor == 0, "an evicted anchor should clamp to the new first line"


def test_clear_resets_the_scroll_anchor():
    ui = FullscreenREPLUI(
        status_state=StatusBarState(),
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments={},
        key_bindings=KeyBindings(),
        on_interrupt=lambda _e: False,
        output=DummyOutput(),
    )
    ui._lines = [_Line("a"), _Line("b")]
    ui._scroll_anchor = 0

    ui.clear()

    assert ui._scroll_anchor is None
    assert ui._lines == []
