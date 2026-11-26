"""Resizing the terminal lays width-dependent output out again.

Panels, tables, replies and diffs used to stay at the width they were first
drawn at, so shrinking the window split their borders across two lines.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import DummyOutput
from rich.panel import Panel
from rich.table import Table

from velune.cli import fullscreen
from velune.cli.fullscreen import FullscreenREPLUI
from velune.cli.statusbar import StatusBarState


class _Output(DummyOutput):
    columns = 100

    def get_size(self) -> Size:
        return Size(rows=30, columns=self.columns)


@pytest.fixture
async def ui():
    instance = FullscreenREPLUI(
        status_state=StatusBarState(),
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments={},
        key_bindings=KeyBindings(),
        on_interrupt=lambda _e: True,
        output=_Output(),
    )
    instance._render_conversation()  # first layout, at 100 columns
    yield instance
    instance.finish_assistant()


def _resize(ui: FullscreenREPLUI, columns: int) -> None:
    ui._app.output.columns = columns
    ui._render_conversation()  # what the next frame does


def _texts(ui: FullscreenREPLUI) -> list[str]:
    return [line.text.rstrip() for line in ui._lines]


def _widest(ui: FullscreenREPLUI) -> int:
    return max(len(text) for text in _texts(ui))


async def test_a_panel_is_redrawn_at_the_new_width(ui):
    ui.console.print(Panel("status panel body " * 6, title="Panel"))
    assert _widest(ui) == 100
    _resize(ui, 60)
    assert _widest(ui) == 60
    body = " ".join(t.strip("│ ") for t in _texts(ui) if t.startswith("│"))
    assert body.split() == ("status panel body " * 6).split()  # nothing cropped


async def test_a_table_grows_back_when_the_window_widens(ui):
    table = Table("File", "Purpose")
    table.add_row("a.py", "a fairly long description " * 3)
    _resize(ui, 50)
    ui.console.print(table)
    narrow = len(ui._lines)
    _resize(ui, 120)
    assert len(ui._lines) < narrow  # rows unwrap again


async def test_a_finished_reply_is_redrawn_at_the_new_width(ui):
    reply = "| File | Purpose |\n|---|---|\n| a.py | " + "long words " * 12 + "|\n"
    ui.begin_assistant()
    ui.update_assistant(reply, final=True)
    ui.finish_assistant()
    _resize(ui, 50)
    expected = [line.text.rstrip() for line in ui._reply_lines(reply, 50)]
    assert _texts(ui)[-len(expected) :] == expected
    assert _widest(ui) <= 50


async def test_a_diff_with_a_redraw_function_is_laid_out_again(ui):
    def render(width):
        return [[("", "-" * width)]]

    ui.append_fragment_lines(render(100), render=render)
    _resize(ui, 70)
    assert _texts(ui) == ["-" * 70]


async def test_plain_text_is_left_for_the_window_to_wrap(ui):
    ui.append_system("x" * 150)
    _resize(ui, 60)
    assert _texts(ui) == ["x" * 150]


async def test_a_block_partly_dropped_by_the_cap_is_not_brought_back(ui, monkeypatch):
    monkeypatch.setattr(fullscreen, "_MAX_TRANSCRIPT_LINES", 3)
    ui.console.print(Panel("one\ntwo\nthree\nfour"))  # 6 lines, cap keeps 3
    kept = _texts(ui)
    _resize(ui, 40)
    assert _texts(ui) == kept


async def test_output_above_a_streaming_reply_is_redrawn_and_the_reply_kept(ui):
    ui.begin_assistant()
    ui.update_assistant("partial answer", final=True)
    ui.console.print(Panel("status " * 30))  # goes above the reply
    _resize(ui, 50)
    start = ui._stream_start
    assert start is not None
    assert "partial answer" in "".join(_texts(ui)[start:])
    assert all(len(t) <= 50 for t in _texts(ui)[:start])


async def test_the_scroll_position_follows_the_redrawn_lines(ui):
    for i in range(40):
        ui.console.print(Panel(f"panel {i} " + "word " * 30))
    target = next(i for i, t in enumerate(_texts(ui)) if "panel 20 " in t)
    ui._scroll_anchor = target
    _resize(ui, 50)
    assert "panel 20 " in _texts(ui)[ui._scroll_anchor]
