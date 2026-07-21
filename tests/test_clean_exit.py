"""Ctrl+C (and any other REPL exit path) must leave nothing behind.

`Application.run_async()` unconditionally repaints the layout one last time in
"done" state when it finishes (`_redraw(render_as_done=True)`, in a `finally`
around the wait for `.exit()` — covers a clean `.exit()`, `/exit`, and an
interrupt/cancellation alike). Without `erase_when_done=True` that final paint
*repaints the full live chrome* (banner, hints, status bar, prompt box) one
more time instead of removing it — confirmed empirically below by diffing the
raw bytes written to the output stream around `ui.stop()`: with the flag off,
the closing sequence is `cursor up N -> erase down -> full frame redraw`; with
it on, it's just `cursor up N -> erase down`, nothing redrawn afterwards. That
leftover repaint is exactly the "prompt lines and UI artifacts left behind
before the shell prompt appears" bug.

Note the renderer's `_last_screen` does *not* distinguish the two cases —
prompt_toolkit's own shutdown path calls `renderer.reset()` unconditionally
right after the done-render, which clears it either way. The only reliable
signal is the actual bytes written to the terminal, which is what these tests
check.

Drives a real `Application` against a real `Vt100_Output` over `StringIO` —
the bug lives in the renderer's shutdown path, not in any widget's rendering.
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
from velune.cli.fullscreen import FullscreenREPLUI
from velune.cli.inline_flow import InlineFlow
from velune.cli.statusbar import StatusBarState

_SETTLE = 0.25

# Chrome text that only appears via a fresh render of the live layout.
_CHROME_MARKERS = ("Enter send", "commands", "NORMAL")


def _build_ui(inp) -> tuple[FullscreenREPLUI, io.StringIO]:
    buf = io.StringIO()
    output = Vt100_Output(buf, lambda: Size(rows=45, columns=160))
    flow = InlineFlow()
    palette = CommandPalette([], suppressed=flow.is_active)
    kb = KeyBindings()
    palette.add_bindings(kb)
    flow.add_bindings(kb)
    ui = FullscreenREPLUI(
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
    return ui, buf


def _exit_delta() -> str:
    async def _main():
        with create_pipe_input() as inp:
            ui, buf = _build_ui(inp)
            ui._running = True
            task = asyncio.ensure_future(ui.run())
            await asyncio.sleep(_SETTLE)

            before = len(buf.getvalue())
            ui.stop()
            await task
            return buf.getvalue()[before:]

    return asyncio.run(_main())


@pytest.mark.timeout(30)
def test_exit_erases_the_live_chrome_instead_of_repainting_it():
    delta = _exit_delta()

    for marker in _CHROME_MARKERS:
        assert marker not in delta, (
            f"exit repainted the live chrome ({marker!r} found in the "
            "post-shutdown bytes) instead of erasing it"
        )
    # An erase-down (`ESC [ J`) must still happen — this isn't "write nothing".
    assert "\x1b[J" in delta


@pytest.mark.timeout(30)
def test_exit_after_a_turn_still_erases_the_chrome():
    """Same guarantee once real conversation content is on screen — the fix
    must only affect the final shutdown paint, not ordinary rendering."""

    async def _main():
        with create_pipe_input() as inp:
            ui, buf = _build_ui(inp)
            ui._running = True
            task = asyncio.ensure_future(ui.run())
            await asyncio.sleep(_SETTLE)

            ui.append_user("hello")
            ui.append_console_line("Saving session...")
            await asyncio.sleep(_SETTLE)

            before = len(buf.getvalue())
            ui.stop()
            await task
            return buf.getvalue()[before:], buf.getvalue()[:before]

    delta, written_before_exit = asyncio.run(_main())

    for marker in _CHROME_MARKERS:
        assert marker not in delta
    # Rendered as part of the ordinary live frame before `ui.stop()` — the
    # shutdown erase only affects what happens *after* that point.
    assert "hello" in written_before_exit
    assert "Saving session" in written_before_exit


@pytest.mark.timeout(30)
def test_erase_when_done_is_enabled():
    """Guards the one-line config directly, so a future refactor that drops
    the kwarg fails fast here instead of only in the byte-diffing tests."""
    with create_pipe_input() as inp:
        ui, _buf = _build_ui(inp)
        assert ui._app.erase_when_done is True
