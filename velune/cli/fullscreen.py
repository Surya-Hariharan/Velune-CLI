"""Fullscreen prompt-toolkit UI for the interactive Velune REPL."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText, FormattedText, to_formatted_text
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FloatContainer, Layout
from prompt_toolkit.layout.containers import Float, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import Validator
from rich.console import Console

from velune.cli import design
from velune.cli.home import HOME_STYLES, HomeState, render_home
from velune.cli.rendering.markdown import CustomMarkdown, MarkdownStreamBuffer
from velune.cli.rendering.segments_to_pt import render_to_fragments
from velune.cli.statusbar import render_status_bar

# General-purpose "is there any escape sequence at all" check, used only to
# decide whether a console line needs the ANSI() parse path in
# append_console_line(); the actual stripping in _ConsoleSink is more
# selective (SGR color codes are preserved, not stripped — see below).
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# OSC sequences (hyperlinks, window title) — never meaningful in this pane.
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
# Full CSI grammar (same as _ANSI_RE) — matches a *complete* sequence so we
# can decide whether to keep it based on its final byte. A naive
# "match everything up to a non-'m' char" pattern would backtrack into the
# middle of a legitimate SGR sequence and truncate it, leaking a stray 'm'.
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_non_sgr_csi(text: str) -> str:
    """Remove CSI sequences that aren't SGR (color/style) — cursor movement,
    erase-line, show/hide-cursor, etc. prompt_toolkit's ANSI() bridge only
    reliably handles SGR; these must not reach it."""
    return _CSI_RE.sub(lambda m: m.group(0) if m.group(0).endswith("m") else "", text)


_MAX_TRANSCRIPT_LINES = 4000
_PROMPT_MAX_LINES = 5
_MARKDOWN_STREAM_THROTTLE_S = 0.08

# Braille spinner frames for the "thinking" indicator — advances every tick so
# the wait feels alive, coloured with the brand accent.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_THINKING_TICK_S = 0.1
_THINKING_WORD_EVERY = 8  # ticks between verb changes (~0.8s)


@dataclass
class _Line:
    text: str
    style: str = "class:conversation"
    # Pre-rendered (style, text) segments — set when this line carries real
    # color/formatting (a parsed console line, or a markdown-rendered
    # streaming line). None means "render `text`/`style` flat," the original
    # behavior.
    fragments: list[tuple[str, str]] | None = field(default=None)


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
        # Best-effort width sync: Rich's Console doesn't know about this
        # app's own terminal-size polling, so a stale console.size would
        # pre-wrap panels/tables to the wrong width before we ever see them.
        # This can only correct the *next* print (Rich buffers segments
        # before flushing to file.write), not the one currently in flight.
        try:
            self._ui.console.size = (self._ui._width(), self._ui._height())
        except Exception:
            pass
        data = _OSC_RE.sub("", data)
        data = _strip_non_sgr_csi(data)
        self._pending += data.replace("\r", "")
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._ui.append_console_line(line.rstrip())
        return len(data)

    def flush(self) -> None:
        if self._pending:
            self._ui.append_console_line(self._pending.rstrip())
            self._pending = ""


def _build_floats(command_palette: Any | None, model_switcher: Any | None = None) -> list[Float]:
    """The CompletionsMenu float (model-id/@@symbol completion), plus the
    command palette's float when one is supplied.

    The palette (`command_palette.CommandPalette`) already has its key
    bindings wired by the caller (`add_bindings`) and its own
    `ConditionalContainer` that only renders while a bare `/command` is being
    typed (`container()`, self-gated on `is_active()`) — the only piece that
    was missing was composing that container into this Application's actual
    layout, which `.attach()` does for a `PromptSession` but has no equivalent
    for a hand-built `Application` like this one. Same Float geometry
    `.attach()` uses internally.
    """
    floats = [Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12))]
    if command_palette is not None:
        floats.append(
            Float(
                content=command_palette.container(),
                left=2,
                right=2,
                top=1,
                height=18,
                allow_cover_cursor=True,
                z_index=20,
            )
        )
    if model_switcher is not None:
        # Upper-right, compact — distinct from the palette's full-width box
        # so the two never visually compete (they're triggered differently:
        # typing "/" vs. Alt+Up/Down).
        floats.append(
            Float(
                content=model_switcher.container(),
                right=2,
                top=1,
                height=14,
                allow_cover_cursor=True,
                z_index=20,
            )
        )
    return floats


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
        command_palette: Any | None = None,
        model_switcher: Any | None = None,
        home_provider: Any | None = None,
        input: Any | None = None,
        output: Any | None = None,
    ) -> None:
        self._status_state = status_state
        self._on_status_render = on_status_render
        # Callable returning a fresh HomeState; rendered while the transcript
        # is empty. None falls back to a minimal wordmark line.
        self._home_provider = home_provider
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._lines: list[_Line] = []
        self._stream_start: int | None = None
        self._stream_text = ""
        self._last_stream_render: float = 0.0
        self._running = False
        self._app: Application[None] | None = None
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
            force_terminal=True,
            color_system="truecolor",
            # `force_interactive=False` keeps Live/console.status() from
            # emitting cursor-repositioning redraw sequences (this pane isn't
            # a real terminal, so "redraw in place" is meaningless here) —
            # without also losing color, which force_terminal=False used to
            # cost us: every Panel/Table/spinner rendered anywhere in the app
            # was rendering as flat, colorless text the instant it passed
            # through this console during a live session.
            force_interactive=False,
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
                "conversation.spinner": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                **HOME_STYLES,
                "separator": f"bg:{design.BACKGROUND} {design.BACKGROUND}",  # Hide separators for minimal look
                "prompt": f"bg:{design.BACKGROUND} {design.WHITE}",
                "prompt.prefix": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "prompt.border": f"bg:{design.BACKGROUND} {design.FAINT}",
                "prompt.hint": f"bg:{design.BACKGROUND} {design.FAINT} italic",
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
                        FormattedTextControl(self._render_prompt_top_border),
                        height=1,
                        always_hide_cursor=True,
                    ),
                    Window(
                        BufferControl(buffer=self.buffer),
                        height=Dimension(min=1, max=_PROMPT_MAX_LINES, preferred=1),
                        wrap_lines=True,
                        style="class:prompt",
                        get_line_prefix=self._prompt_line_prefix,
                    ),
                    Window(
                        FormattedTextControl(self._render_prompt_bottom_border),
                        height=1,
                        always_hide_cursor=True,
                    ),
                ]
            ),
            floats=_build_floats(command_palette, model_switcher),
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
            # No idle polling: every state change (submit, console output,
            # streaming, the thinking-spinner loop, interrupts) already calls
            # invalidate() explicitly, so a periodic refresh only burned CPU and
            # re-ran the status/home render callbacks 4x/second while idle.
            refresh_interval=None,
            input=input,
            output=output,
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
        if not self._lines:
            # First message of the session — vary the thinking verbs once.
            import random

            random.shuffle(self._thinking_words)
        self.append_user(text)
        self._queue.put_nowait(text)
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
        if "\x1b" in line:
            # Parse once, at append time, not on every _render_conversation()
            # call (which can run many times/sec under min_redraw_interval).
            plain = _ANSI_RE.sub("", line)
            fragments = to_formatted_text(ANSI(line))
            self._lines.append(_Line(plain, "class:conversation.system", fragments=fragments))
        else:
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
        self._thinking_idx = 0
        self._stream_text = self._thinking_words[0]
        self._set_thinking_line(_SPINNER_FRAMES[0], self._stream_text)
        self.invalidate()

        async def _thinking_anim():
            tick = 0
            while True:
                await asyncio.sleep(_THINKING_TICK_S)
                tick += 1
                if tick % _THINKING_WORD_EVERY == 0:
                    self._thinking_idx = (self._thinking_idx + 1) % len(self._thinking_words)
                    self._stream_text = self._thinking_words[self._thinking_idx]
                self._set_thinking_line(_SPINNER_FRAMES[tick % len(_SPINNER_FRAMES)], self._stream_text)
                self.invalidate()

        from velune.core.task_registry import track

        self._thinking_task = track(
            asyncio.create_task(_thinking_anim(), name="velune.fullscreen_thinking")
        )

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

        # Throttle the markdown parse + syntax-highlight pass — it's real
        # work (not a flat string split), so doing it on every raw chunk
        # would be wasteful. `final` always renders immediately.
        now = time.perf_counter()
        if not final and (now - self._last_stream_render) < _MARKDOWN_STREAM_THROTTLE_S:
            return
        self._last_stream_render = now
        self._render_stream_markdown(final=final)
        self.invalidate()

    def finish_assistant(self) -> None:
        if self._thinking_task and not self._thinking_task.done():
            self._thinking_task.cancel()
            self._thinking_task = None

        if self._stream_start is not None:
            self._render_stream_markdown(final=True)
        self._stream_start = None
        self._stream_text = ""
        self.invalidate()

    def _render_stream_markdown(self, *, final: bool) -> None:
        """Render the in-flight streamed response as real markdown + syntax-
        highlighted code, using `MarkdownStreamBuffer`'s flicker-safe partial-
        fence stabilization. Falls back to flat text if rendering fails —
        streaming must never break because of a markdown-parse edge case.
        """
        start = self._stream_start
        if start is None:
            return
        text = self._stream_text or "..."
        style = "class:conversation.assistant" if final else "class:conversation"
        try:
            stabilized = MarkdownStreamBuffer._stabilize(text)
            line_fragments = render_to_fragments(
                self.console, CustomMarkdown(stabilized), self._width()
            )
        except Exception:
            self._lines[start:] = []
            self._append_wrapped(text, style)
            self._trim()
            return

        self._lines[start:] = []
        if not line_fragments:
            self._lines.append(_Line("", style))
        else:
            for frags in line_fragments:
                plain = "".join(t for _s, t in frags)
                self._lines.append(_Line(plain, style, fragments=frags))
        self._trim()

    def clear(self) -> None:
        self._lines.clear()
        self._stream_start = None
        self._stream_text = ""
        self.invalidate()

    def _set_thinking_line(self, spinner: str, word: str) -> None:
        """Render the thinking indicator as a single line: an accent-coloured
        spinner glyph followed by the current verb in muted italic."""
        start = self._stream_start
        if start is None:
            return
        self._lines[start:] = []
        frags = [
            ("class:conversation.spinner", f"{spinner} "),
            ("class:conversation.thinking", word),
        ]
        self._lines.append(_Line(f"{spinner} {word}", "class:conversation.thinking", fragments=frags))
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

    def _render_prompt_top_border(self) -> AnyFormattedText:
        width = self._width()
        return FormattedText([("class:prompt.border", "╭" + "─" * max(1, width - 2) + "╮")])

    def _render_prompt_bottom_border(self) -> AnyFormattedText:
        width = self._width()
        hint = "Enter send  ·  Shift+Enter newline  ·  / commands  ·  @@ files"
        fill = width - 5 - len(hint)
        if fill >= 1:
            return FormattedText(
                [
                    ("class:prompt.border", "╰─ "),
                    ("class:prompt.hint", hint),
                    ("class:prompt.border", " " + "─" * fill + "╯"),
                ]
            )
        return FormattedText([("class:prompt.border", "╰" + "─" * max(1, width - 2) + "╯")])

    def _prompt_line_prefix(self, line_number: int, wrap_count: int) -> AnyFormattedText:
        # First visual line gets the prompt glyph; wrapped/continuation
        # lines get matching blank padding so multi-line input stays aligned
        # inside the border instead of flush against the left edge.
        if line_number == 0 and wrap_count == 0:
            return FormattedText([("class:prompt.border", "│ "), ("class:prompt.arrow", "❯ ")])
        return FormattedText([("class:prompt.border", "│ "), ("", "  ")])

    def _render_conversation(self) -> AnyFormattedText:
        if not self._lines:
            return self._render_home()

        available = max(1, self._height() - (_PROMPT_MAX_LINES + 3))
        tail = self._lines[-available:]
        fragments: list[tuple[str, str]] = []
        for idx, line in enumerate(tail):
            if line.fragments is not None:
                fragments.extend(line.fragments)
                sep_style = ""
            else:
                fragments.append((line.style, line.text))
                sep_style = line.style
            if idx < len(tail) - 1:
                fragments.append((sep_style, "\n"))
        return FormattedText(fragments)

    def _render_home(self) -> AnyFormattedText:
        """Compact upper-left header + runtime summary for the empty transcript."""
        if self._home_provider is not None:
            try:
                state = self._home_provider()
                if state is not None:
                    return render_home(state, self._width())
            except Exception:
                pass
        return render_home(HomeState(), self._width())

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
