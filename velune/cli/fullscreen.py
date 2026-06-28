"""Fullscreen prompt-toolkit UI for the interactive Velune REPL."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import AnyFormattedText, FormattedText
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FloatContainer, Layout
from prompt_toolkit.layout.containers import Float, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import Validator
from rich.console import Console

from velune.cli import design
from velune.cli.banner import _LOGO_ART
from velune.cli.statusbar import render_status_bar

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_TRANSCRIPT_LINES = 4000
_PROMPT_MAX_LINES = 5


@dataclass
class _Line:
    text: str
    style: str = "class:conversation"


class _ConsoleSink:
    """File-like sink that routes Rich console output into the conversation pane."""

    encoding = "utf-8"

    def __init__(self, ui: FullscreenREPLUI) -> None:
        self._ui = ui
        self._pending = ""

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._pending += _ANSI_RE.sub("", data).replace("\r", "")
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._ui.append_console_line(line.rstrip())
        return len(data)

    def flush(self) -> None:
        if self._pending:
            self._ui.append_console_line(self._pending.rstrip())
            self._pending = ""


class FullscreenREPLUI:
    """Owns the alternate-screen UI, prompt buffer, status line, and transcript."""

    def __init__(
        self,
        *,
        status_state: Any,
        history: History,
        completer: Completer | None,
        validator: Validator | None,
        style_fragments: dict[str, str],
        key_bindings: KeyBindings,
        on_interrupt: Any,
        on_status_render: Any | None = None,
    ) -> None:
        self._status_state = status_state
        self._on_status_render = on_status_render
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._lines: list[_Line] = []
        self._stream_start: int | None = None
        self._stream_text = ""
        self._running = False
        self._app: Application[None] | None = None
        self._logo_offset: float = 0.0
        self._logo_animating = False
        self._thinking_task: asyncio.Task | None = None
        self._thinking_idx: int = 0
        self._thinking_words = [
            "Thinking...",
            "Planning...",
            "Connecting ideas...",
            "Organizing thoughts...",
            "Brewing...",
            "Cooking...",
            "Polishing...",
            "Exploring...",
            "Synthesizing...",
            "Refactoring...",
            "Mapping...",
            "Reflecting...",
            "Optimizing...",
            "Calibrating...",
            "Cross-checking...",
            "Inspecting...",
            "Verifying...",
            "Composing...",
        ]

        def _on_accept(buffer: Buffer) -> bool:
            text = buffer.text
            if text.strip():
                self.submit(text)
                buffer.reset(Document(""))
            return True

        self.buffer = Buffer(
            history=history,
            completer=completer,
            auto_suggest=AutoSuggestFromHistory(),
            validator=validator,
            complete_while_typing=True,
            validate_while_typing=True,
            multiline=True,
            accept_handler=_on_accept,
        )

        self.console = Console(
            file=_ConsoleSink(self),
            force_terminal=False,
            color_system=None,
            highlight=False,
            soft_wrap=True,
        )

        style = dict(style_fragments)
        style.update(
            {
                "": f"bg:{design.BACKGROUND} {design.WHITE}",
                "conversation": f"bg:{design.BACKGROUND} {design.WHITE}",
                "conversation.dim": f"bg:{design.BACKGROUND} {design.SECONDARY}",
                "conversation.user": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "conversation.assistant": f"bg:{design.BACKGROUND} {design.WHITE}",
                "conversation.system": f"bg:{design.BACKGROUND} {design.SECONDARY}",
                "conversation.thinking": f"bg:{design.BACKGROUND} {design.SECONDARY} italic",
                "logo.pink": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "logo.white": f"bg:{design.BACKGROUND} {design.WHITE} bold",
                "separator": f"bg:{design.BACKGROUND} {design.BACKGROUND}",  # Hide separators for minimal look
                "prompt": f"bg:{design.BACKGROUND} {design.WHITE}",
                "prompt.prefix": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "prompt.placeholder": f"bg:{design.BACKGROUND} {design.SECONDARY}",
            }
        )

        kb = key_bindings

        @kb.add("enter")
        def _(event) -> None:
            event.current_buffer.validate_and_handle()

        def _insert_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        kb.add("escape", "enter", eager=True)(_insert_newline)
        kb.add("c-j", eager=True)(_insert_newline)
        try:
            kb.add("s-enter", eager=True)(_insert_newline)
        except ValueError:
            # Many terminals do not distinguish Shift+Enter at the protocol
            # layer; Esc+Enter and Ctrl+J remain explicit newline fallbacks.
            pass

        @kb.add("c-c", eager=True)
        def _(event) -> None:
            if event.current_buffer.text:
                event.current_buffer.reset(Document(""))
            else:
                should_exit = bool(on_interrupt(event))
                if should_exit:
                    self._queue.put_nowait("/exit")

        root = FloatContainer(
            content=HSplit(
                [
                    Window(
                        FormattedTextControl(self._render_conversation),
                        style="class:conversation",
                        wrap_lines=True,
                        always_hide_cursor=True,
                    ),
                    Window(
                        FormattedTextControl(self._render_status),
                        height=1,
                        style="class:conversation",
                        always_hide_cursor=True,
                    ),
                    Window(
                        FormattedTextControl(self._render_separator),
                        height=1,
                        always_hide_cursor=True,
                    ),
                    Window(
                        BufferControl(
                            buffer=self.buffer,
                            input_processors=[
                                ConditionalProcessor(
                                    BeforeInput(
                                        FormattedText(
                                            [
                                                (
                                                    "class:prompt.placeholder",
                                                    "Type your request... ",
                                                )
                                            ]
                                        )
                                    ),
                                    Condition(lambda: not self.buffer.text),
                                )
                            ],
                        ),
                        height=Dimension(min=1, max=_PROMPT_MAX_LINES, preferred=1),
                        wrap_lines=True,
                        style="class:prompt",
                    ),
                    Window(
                        FormattedTextControl(self._render_separator),
                        height=1,
                        always_hide_cursor=True,
                    ),
                ]
            ),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12))],
        )

        self._app = Application(
            layout=Layout(root, focused_element=self.buffer),
            key_bindings=kb,
            style=Style.from_dict(style),
            full_screen=True,
            erase_when_done=False,
            mouse_support=False,
            min_redraw_interval=0.016,
            max_render_postpone_time=0.01,
            refresh_interval=0.25,
        )

    async def run(self) -> None:
        if self._app is None:
            return
        self._running = True
        try:
            await self._app.run_async()
        finally:
            self._running = False

    def stop(self) -> None:
        if self._app is not None and self._running:
            self._app.exit()

    async def read_input(self) -> str:
        return await self._queue.get()

    def request_exit(self) -> None:
        self._queue.put_nowait("/exit")

    def submit(self, text: str) -> None:
        if not self._lines and not self._logo_animating:
            self._logo_animating = True
            asyncio.create_task(self._animate_logo_out(text))
        else:
            self.append_user(text)
            self._queue.put_nowait(text)
            self.invalidate()

    async def _animate_logo_out(self, text: str) -> None:
        import random

        # Randomize thinking words at the start of the session
        random.shuffle(self._thinking_words)

        # Slide up animation (200-350ms)
        steps = 15
        delay = 0.25 / steps
        for _i in range(steps):
            self._logo_offset += 1.5
            self.invalidate()
            await asyncio.sleep(delay)

        self.append_user(text)
        self._queue.put_nowait(text)
        self._logo_animating = False
        self.invalidate()

    def invalidate(self) -> None:
        app = self._app or get_app_or_none()
        if app is not None:
            app.invalidate()

    def append_user(self, text: str) -> None:
        self._append_gap()
        self._lines.append(_Line("You", "class:conversation.user"))
        self._append_wrapped(text, "class:conversation")
        self._trim()

    def append_console_line(self, line: str) -> None:
        if not line and (not self._lines or not self._lines[-1].text):
            return
        self._lines.append(_Line(line, "class:conversation.system"))
        self._trim()
        self.invalidate()

    def append_system(self, text: str) -> None:
        self._append_wrapped(text, "class:conversation.system")
        self._trim()
        self.invalidate()

    def begin_assistant(self, text: str = "Thinking...") -> None:
        self._append_gap()
        self._lines.append(_Line("Velune", "class:conversation.user"))
        self._stream_start = len(self._lines)
        self._stream_text = self._thinking_words[0]
        self._replace_stream_lines(self._stream_text, "class:conversation.thinking")
        self.invalidate()

        async def _thinking_anim():
            while True:
                await asyncio.sleep(0.6)
                self._thinking_idx = (self._thinking_idx + 1) % len(self._thinking_words)
                self._stream_text = self._thinking_words[self._thinking_idx]
                self._replace_stream_lines(self._stream_text, "class:conversation.thinking")
                self.invalidate()

        self._thinking_task = asyncio.create_task(_thinking_anim())

    def update_assistant(self, text: str, *, final: bool = False) -> None:
        if self._thinking_task and not self._thinking_task.done():
            self._thinking_task.cancel()
            self._thinking_task = None

        if self._stream_start is None:
            self.begin_assistant("")
            if self._thinking_task:
                self._thinking_task.cancel()
                self._thinking_task = None

        self._stream_text = text
        style = "class:conversation.assistant" if final else "class:conversation"
        self._replace_stream_lines(text or "...", style)
        self.invalidate()

    def finish_assistant(self) -> None:
        if self._thinking_task and not self._thinking_task.done():
            self._thinking_task.cancel()
            self._thinking_task = None

        if self._stream_start is not None:
            self._replace_stream_lines(self._stream_text, "class:conversation.assistant")
        self._stream_start = None
        self._stream_text = ""
        self.invalidate()

    def clear(self) -> None:
        self._lines.clear()
        self._stream_start = None
        self._stream_text = ""
        self.invalidate()

    def _replace_stream_lines(self, text: str, style: str) -> None:
        start = self._stream_start
        if start is None:
            return
        self._lines[start:] = []
        self._append_wrapped(text, style)
        self._trim()

    def _append_gap(self) -> None:
        if self._lines and self._lines[-1].text:
            self._lines.append(_Line(""))

    def _append_wrapped(self, text: str, style: str) -> None:
        for raw in (text or "").splitlines() or [""]:
            self._lines.append(_Line(raw.rstrip(), style))

    def _trim(self) -> None:
        if len(self._lines) > _MAX_TRANSCRIPT_LINES:
            overflow = len(self._lines) - _MAX_TRANSCRIPT_LINES
            self._lines = self._lines[overflow:]
            if self._stream_start is not None:
                self._stream_start = max(0, self._stream_start - overflow)

    def _render_status(self) -> AnyFormattedText:
        if self._on_status_render is not None:
            self._on_status_render()
        return render_status_bar(self._status_state)

    def _render_separator(self) -> AnyFormattedText:
        width = self._width()
        return FormattedText([("class:separator", "─" * max(1, width))])

    def _render_conversation(self) -> AnyFormattedText:
        if not self._lines and not self._logo_animating:
            return self._render_logo()
        elif self._logo_animating and not self._lines:
            return self._render_logo()

        available = max(1, self._height() - (_PROMPT_MAX_LINES + 3))
        tail = self._lines[-available:]
        fragments: list[tuple[str, str]] = []
        for idx, line in enumerate(tail):
            fragments.append((line.style, line.text))
            if idx < len(tail) - 1:
                fragments.append((line.style, "\n"))
        return FormattedText(fragments)

    def _render_logo(self) -> AnyFormattedText:
        width = self._width()
        height = self._height()

        max(len(line) for letter in _LOGO_ART for line in letter)  # Approximate
        logo_lines = [""] * 6
        for row in range(6):
            for _i, letter in enumerate(_LOGO_ART):
                logo_lines[row] += letter[row]

        actual_logo_width = max(len(line) for line in logo_lines)
        left = " " * max(0, (width - actual_logo_width) // 2)

        # Calculate base top, then subtract animation offset
        base_top_lines = max(0, (height - len(logo_lines) - 7) // 2)
        anim_top_lines = max(0, int(base_top_lines - self._logo_offset))

        top = "\n" * anim_top_lines

        fragments: list[tuple[str, str]] = [("class:conversation", top)]

        for _row, line in enumerate(logo_lines):
            fragments.append(("class:conversation", left))

            # The 'V' is the first letter, which is 9 chars wide in _LOGO_ART[0]
            # _LOGO_ART[0] lines are 9 chars. We color the V pink, rest white.
            v_part = line[:9]
            rest_part = line[9:]

            fragments.append(("class:logo.pink", v_part))
            fragments.append(("class:logo.white", rest_part))
            fragments.append(("class:conversation", "\n"))

        return FormattedText(fragments)

    def _width(self) -> int:
        if self._app is None:
            return 80
        try:
            return max(20, self._app.output.get_size().columns)
        except Exception:
            return 80

    def _height(self) -> int:
        if self._app is None:
            return 24
        try:
            return max(10, self._app.output.get_size().rows)
        except Exception:
            return 24
