"""The composer's grow-then-freeze height contract, and per-region mouse
wheel scroll isolation between the composer and the conversation pane.

Drives a real `Application` against a real `Vt100_Output` over `StringIO`
(same harness as `test_scrollback_navigation.py`) and reads
`Window.render_info.window_height` off the live layout — not just the return
value of `_prompt_window_height()` in isolation. That distinction matters: a
previous version of this box set a "correct" `Dimension(preferred=1)` but
still rendered pinned to its max in any terminal with a few spare rows,
because `HSplit._divide_heights()` hands unclaimed terminal height to any
child still below its own `max`, regardless of what its `preferred` says.
Only `dont_extend_height=True` (which makes `Window.preferred_height()`
report `max == preferred`) stops that redistribution from reaching the
composer — so these tests render for real to catch a regression of exactly
that kind, not just a change to the height-calculation formula.
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

from velune.cli.fullscreen import FullscreenREPLUI
from velune.cli.statusbar import StatusBarState

_SETTLE = 0.25
_ROWS = 20
_COLUMNS = 100


def _build_ui(inp, *, min_lines: int | None = None, max_lines: int | None = None) -> FullscreenREPLUI:
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
        composer_min_lines=min_lines,
        composer_max_lines=max_lines,
    )


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


def _composer_height(ui: FullscreenREPLUI) -> int:
    info = ui._prompt_window.render_info
    assert info is not None
    return info.window_height


def _sgr_scroll(x: int, y: int, *, up: bool) -> bytes:
    """An SGR-mode xterm mouse-wheel report at 0-based (x, y)."""
    code = 64 if up else 65
    return f"\x1b[<{code};{x + 1};{y + 1}M".encode()


@pytest.mark.timeout(30)
def test_idle_composer_renders_at_configured_minimum_not_max():
    async def _body(ui: FullscreenREPLUI, _inp):
        return _composer_height(ui)

    height = _run(_body)
    assert height == 3, (
        "an empty composer in a roomy 20-row terminal must stay at its "
        "min (3), not balloon to its max via HSplit's leftover-space "
        "redistribution — this is the regression the old static "
        "Dimension(preferred=1) suffered from"
    )


@pytest.mark.timeout(30)
def test_composer_grows_with_multiline_content_below_max():
    async def _body(ui: FullscreenREPLUI, _inp):
        ui.buffer.insert_text("one\ntwo\nthree\nfour\nfive")  # 5 logical lines
        await asyncio.sleep(_SETTLE)
        return _composer_height(ui)

    height = _run(_body)
    assert height == 5


@pytest.mark.timeout(30)
def test_composer_freezes_at_max_and_does_not_shrink_back_mid_draft():
    async def _body(ui: FullscreenREPLUI, _inp):
        ui.buffer.insert_text("\n".join(str(i) for i in range(15)))  # 15 lines, past max(8)
        await asyncio.sleep(_SETTLE)
        at_max = _composer_height(ui)
        frozen_flag = ui._prompt_frozen_at_max

        # Delete back down to well under max — a real editor session would
        # do this while still composing the same message.
        ui.buffer.text = "short"
        ui.buffer.cursor_position = len(ui.buffer.text)
        await asyncio.sleep(_SETTLE)
        after_shrink_attempt = _composer_height(ui)

        return at_max, frozen_flag, after_shrink_attempt

    at_max, frozen_flag, after_shrink_attempt = _run(_body)
    assert at_max == 8
    assert frozen_flag is True
    assert after_shrink_attempt == 8, "outer height must not change once frozen for this draft"


@pytest.mark.timeout(30)
def test_large_paste_freezes_immediately_without_exceeding_max():
    async def _body(ui: FullscreenREPLUI, _inp):
        pasted = "\n".join(f"line {i}" for i in range(2000))
        ui.buffer.insert_text(pasted)
        await asyncio.sleep(_SETTLE)
        return _composer_height(ui), ui._prompt_frozen_at_max, len(ui.buffer.text)

    height, frozen, text_len = _run(_body)
    assert height == 8, "a 2000-line paste must never resize the composer past its max"
    assert frozen is True
    assert text_len > 10_000, "the full paste must still be in the buffer, only the viewport is clipped"


@pytest.mark.timeout(30)
def test_submitting_resets_the_composer_back_to_minimum():
    async def _body(ui: FullscreenREPLUI, _inp):
        ui.buffer.insert_text("\n".join(str(i) for i in range(15)))
        await asyncio.sleep(_SETTLE)
        assert _composer_height(ui) == 8

        ui.buffer.validate_and_handle()  # same path Enter drives
        await asyncio.sleep(_SETTLE)
        return _composer_height(ui), ui._prompt_frozen_at_max, ui.buffer.text

    height, frozen, text = _run(_body)
    assert height == 3
    assert frozen is False
    assert text == ""


@pytest.mark.timeout(30)
def test_custom_min_max_are_honored():
    async def _main():
        with create_pipe_input() as inp:
            ui = _build_ui(inp, min_lines=2, max_lines=4)
            ui._running = True
            task = asyncio.ensure_future(ui.run())
            await asyncio.sleep(_SETTLE)
            try:
                idle = _composer_height(ui)
                ui.buffer.insert_text("\n".join(str(i) for i in range(10)))
                await asyncio.sleep(_SETTLE)
                capped = _composer_height(ui)
                return idle, capped
            finally:
                ui.stop()
                await task

    idle, capped = asyncio.run(_main())
    assert idle == 2
    assert capped == 4


@pytest.mark.timeout(30)
def test_mouse_wheel_over_composer_does_not_scroll_conversation():
    async def _body(ui: FullscreenREPLUI, inp):
        for i in range(_ROWS * 3):
            ui.append_system(f"line-{i}")
        await asyncio.sleep(_SETTLE)
        assert ui._scroll_anchor is None  # pinned to the live tail

        # Layout at rest: conversation rows [0, 13], status row 14, top
        # border 15, composer rows [16, 18], bottom border 19 (14 + 1 + 1 +
        # 3 + 1 == _ROWS). Scroll inside the composer's own rows.
        inp.send_bytes(_sgr_scroll(x=5, y=17, up=True))
        await asyncio.sleep(_SETTLE)
        return ui._scroll_anchor

    anchor = _run(_body)
    assert anchor is None, "wheel scroll over the composer must never move the conversation viewport"


@pytest.mark.timeout(30)
def test_mouse_wheel_over_conversation_scrolls_only_conversation():
    async def _body(ui: FullscreenREPLUI, inp):
        for i in range(_ROWS * 3):
            ui.append_system(f"line-{i}")
        await asyncio.sleep(_SETTLE)
        assert ui._scroll_anchor is None

        inp.send_bytes(_sgr_scroll(x=5, y=5, up=True))  # inside conversation rows [0, 13]
        await asyncio.sleep(_SETTLE)
        return ui._scroll_anchor, ui.buffer.text

    anchor, buffer_text = _run(_body)
    assert anchor is not None, "wheel scroll over the conversation must move its own viewport"
    assert buffer_text == "", "and must never leak into the composer's buffer"


@pytest.mark.timeout(30)
def test_up_down_arrows_move_cursor_within_multiline_text_before_history():
    async def _body(ui: FullscreenREPLUI, inp):
        ui.buffer.insert_text("alpha\nbeta\ngamma")  # cursor lands at end, row 2
        await asyncio.sleep(_SETTLE)
        row_before = ui.buffer.document.cursor_position_row
        text_before = ui.buffer.text

        inp.send_bytes(b"\x1b[A")  # Up
        await asyncio.sleep(_SETTLE)
        row_after_one_up = ui.buffer.document.cursor_position_row
        text_after_one_up = ui.buffer.text

        inp.send_bytes(b"\x1b[A")  # Up again — now at row 0
        await asyncio.sleep(_SETTLE)
        row_after_two_up = ui.buffer.document.cursor_position_row

        inp.send_bytes(b"\x1b[B")  # Down — back toward row 1
        await asyncio.sleep(_SETTLE)
        row_after_down = ui.buffer.document.cursor_position_row

        return row_before, text_before, row_after_one_up, text_after_one_up, row_after_two_up, row_after_down

    (
        row_before,
        text_before,
        row_after_one_up,
        text_after_one_up,
        row_after_two_up,
        row_after_down,
    ) = _run(_body)

    assert row_before == 2
    assert row_after_one_up == 1, "Up should move the cursor up one line within the buffer"
    assert text_after_one_up == text_before, "cursor navigation must not alter the text itself"
    assert row_after_two_up == 0
    assert row_after_down == 1, "Down should move the cursor back down within the same text"
