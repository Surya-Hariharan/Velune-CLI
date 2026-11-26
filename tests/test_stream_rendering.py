"""Streaming a reply into the full-screen transcript.

Covers three faults found by driving the UI on a simulated terminal:
- a line printed while a reply streamed was wiped by the next update;
- the transcript cap could delete the start of the reply still being written;
- every update re-rendered the whole reply (0.4 s per update at 40k chars).
"""

from __future__ import annotations

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import DummyOutput

from velune.cli import fullscreen
from velune.cli.fullscreen import FullscreenREPLUI, stable_markdown_prefix
from velune.cli.statusbar import StatusBarState


class _Output(DummyOutput):
    def get_size(self) -> Size:
        return Size(rows=30, columns=90)


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
    yield instance
    instance.finish_assistant()  # stops the spinner task


def _reply_lines(ui: FullscreenREPLUI) -> list:
    """Visible content of the reply: text plus style, ignoring trailing spaces."""
    start = ui._stream_start or 0
    return [
        [
            (s, t.rstrip())
            for s, t in (line.fragments or [("", line.text)])
            if t.strip() or "bg:" in s
        ]
        for line in ui._lines[start:]
    ]


REPLY = """# Plan

Here is the plan for **the change**, with `inline code`.

1. First step that is rather long and wraps across the terminal width for sure yes
2. Second step

   Continued paragraph inside item two.

3. Third step

```python
def f(x):

    return x * 2
```

| File | Purpose |
|---|---|
| a.py | does a |

> A quote

Final paragraph.
"""


def _render(ui: FullscreenREPLUI, text: str, *, final: bool) -> None:
    ui._stream_text = text
    ui._render_stream_markdown(final=final)


async def test_streaming_in_pieces_renders_exactly_like_one_render(ui):
    ui.begin_assistant()
    _render(ui, REPLY, final=True)
    whole = _reply_lines(ui)

    ui.begin_assistant()
    for end in range(1, len(REPLY) + 1, 5):
        _render(ui, REPLY[:end], final=False)
    _render(ui, REPLY, final=True)
    assert _reply_lines(ui) == whole


async def test_a_line_printed_mid_reply_is_kept_above_the_reply(ui):
    ui.begin_assistant()
    _render(ui, "partial answer", final=False)
    ui.console.print("WARNING printed mid-stream")
    _render(ui, "partial answer, continued", final=True)
    texts = [line.text for line in ui._lines]
    warning = next(i for i, t in enumerate(texts) if "WARNING" in t)
    answer = next(i for i, t in enumerate(texts) if "partial answer, continued" in t)
    assert warning < answer


async def test_the_cap_never_cuts_the_reply_being_written(ui, monkeypatch):
    monkeypatch.setattr(fullscreen, "_MAX_TRANSCRIPT_LINES", 50)
    ui.append_user("question")
    ui.begin_assistant()
    _render(ui, "\n\n".join(f"item {i}" for i in range(200)), final=False)
    texts = [line.text.strip() for line in ui._lines]
    assert "item 0" in texts and "item 199" in texts


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("one\n\ntwo\n", len("one\n\n")),
        ("one\n\n", 0),  # nothing after the blank line yet
        ("one\n\n3", 0),  # next line still arriving: "3" may become "3. an item"
        ("```\na\n\nb\n```\n\nnext\n", len("```\na\n\nb\n```\n\n")),  # not inside a fence
        ("```\na\n\nb", 0),  # still inside an open fence
        ("1. a\n\n   more of a\n\nnext\n", len("1. a\n\n   more of a\n\n")),  # not before indent
        ("1. a\n\n2. b\n", 0),  # not between list items
        ("$$\nx\n\ny\n$$\n\nnext\n", len("$$\nx\n\ny\n$$\n\n")),  # not inside display math
    ],
)
def test_stable_prefix_cuts_only_between_finished_blocks(text, expected):
    assert stable_markdown_prefix(text) == expected


# ── --plain mode: a real terminal, emulated ─────────────────────────────────


def _scrollback(write) -> list[str]:
    """Everything a user could scroll through after *write* ran, read back with pyte."""
    import io

    import pyte
    from rich.console import Console

    width, height = 80, 20
    buffer = io.StringIO()
    console = Console(
        file=buffer, force_terminal=True, width=width, height=height, color_system=None
    )
    write(console)
    screen = pyte.HistoryScreen(width, height, history=5000)
    screen.set_mode(pyte.modes.LNM)
    pyte.Stream(screen).feed(buffer.getvalue())
    history = ["".join(line[x].data for x in range(width)).rstrip() for line in screen.history.top]
    rows = [row.rstrip() for row in screen.display]
    while rows and not rows[-1]:
        rows.pop()
    return history + rows


def test_plain_streaming_leaves_the_same_scrollback_as_printing_once():
    from velune.cli.rendering import CustomMarkdown, MarkdownLiveStream

    reply = (REPLY + "\n") * 3  # taller than the 20-row screen

    def streamed(console):
        stream = MarkdownLiveStream(console, min_interval=0)
        for start in range(0, len(reply), 9):
            stream.append(reply[start : start + 9])
        stream.finish()

    expected = _scrollback(lambda console: console.print(CustomMarkdown(reply)))
    got = _scrollback(streamed)
    assert got == expected
    assert sum("Final paragraph" in line for line in got) == 3  # was 57 with the old Live
