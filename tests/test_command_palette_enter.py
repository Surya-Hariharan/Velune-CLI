"""Enter in the command palette: matches run, non-matches are handed to the REPL.

Regression: with zero matches the palette's Enter handler returned without doing
anything, so `/zzzbogus` + Enter left a dead "No matching commands" popup and no
feedback. The REPL already reports "Unknown command: /x"; the palette must let
the text through so it can. (Real-terminal coverage lives in tests/tui.)
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput

from velune.cli.command_palette import CommandPalette
from velune.cli.slash_commands import SlashCommand


async def _noop(args: str) -> None:
    pass


def _palette() -> CommandPalette:
    cmds = [
        SlashCommand(
            name=n,
            aliases=[],
            description="d",
            usage=f"/{n}",
            handler=_noop,
            category="System",
            search_terms=(),
        )
        for n in ("help", "exit")
    ]
    return CommandPalette(cmds)


async def _type(keys: str) -> tuple[list[str], str]:
    """Type *keys* into a buffer wired to the palette; return (submitted, leftover)."""
    submitted: list[str] = []
    palette = _palette()
    with create_pipe_input() as inp:
        buf = Buffer(accept_handler=lambda b: submitted.append(b.text) or False)
        kb = KeyBindings()
        palette.add_bindings(kb)

        @kb.add("c-q")
        def _quit(event) -> None:
            event.app.exit()

        app = Application(
            layout=Layout(Window(BufferControl(buf))),
            key_bindings=kb,
            input=inp,
            output=DummyOutput(),
        )
        task = asyncio.ensure_future(app.run_async())
        await asyncio.sleep(0.15)
        inp.send_text(keys)
        await asyncio.sleep(0.3)
        leftover = buf.text
        inp.send_text("\x11")
        await asyncio.wait_for(task, 5)
    return submitted, leftover


async def test_enter_with_no_match_submits_the_text_and_clears_the_composer():
    submitted, leftover = await _type("/zzzbogus\r")
    assert submitted == ["/zzzbogus"]
    assert leftover == ""


async def test_enter_with_a_match_still_runs_the_selected_command():
    submitted, leftover = await _type("/he\r")
    assert submitted == ["/help"]
    assert leftover == ""
