"""Fullscreen prompt-toolkit UI for the interactive Velune REPL."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory, ConditionalAutoSuggest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText, FormattedText, to_formatted_text
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FloatContainer, Layout
from prompt_toolkit.layout.containers import Float, HorizontalAlign, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import ConditionalProcessor, PasswordProcessor
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import Validator
from rich.console import Console, ConsoleDimensions

from velune.cli import design
from velune.cli.clipboard import copy_to_clipboard, extract_last_code_block
from velune.cli.home import HomeState, home_styles, render_home
from velune.cli.rendering.markdown import CustomMarkdown, MarkdownStreamBuffer
from velune.cli.rendering.segments_to_pt import render_to_fragments
from velune.cli.statusbar import render_status_bar

_log = logging.getLogger("velune.cli.fullscreen")

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
# Baseline/idle composer height and the ceiling it grows to before freezing
# — see `FullscreenREPLUI._prompt_window_height`. Overridable via
# `display.composer_min_lines` / `display.composer_max_lines` in
# velune.toml (threaded through as the `composer_min_lines`/
# `composer_max_lines` constructor args below).
_PROMPT_MIN_LINES_DEFAULT = 3
_PROMPT_MAX_LINES_DEFAULT = 8
# Prefix width consumed by `_prompt_line_prefix` on every visual line
# ("│ " border + either "❯ " or two spaces of padding) — subtracted from
# the content width when estimating how many visual rows the buffer's text
# will wrap to. An estimate, not a re-implementation of prompt_toolkit's
# real wrapping, so it deliberately ignores wide/combining characters, same
# rigor level as the border-width math in `_render_prompt_top_border`.
_PROMPT_LINE_PREFIX_WIDTH = 4
_MARKDOWN_STREAM_THROTTLE_S = 0.08

# By default the REPL's content column fills the entire terminal width — no
# cap. Set `display.content_max_width` in velune.toml (or
# `VELUNE_DISPLAY__CONTENT_MAX_WIDTH`) to a column count to letterbox the
# conversation/borders/banner into a narrower reading column instead, leaving
# the rest of a wide terminal as empty side gutters. See
# `FullscreenREPLUI.__init__`'s `max_content_width` param, which is what
# threads a configured value through to `_width()` below.

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


@contextlib.contextmanager
def _muted_console_logging():
    """Keep stray log lines off the screen while the full-screen app owns the terminal.

    Library warnings (stderr handler) and structlog's default stdout printer
    otherwise get written straight over the composer. File handlers are left
    alone, so nothing is lost from the log files; everything is restored on exit.
    """
    root = logging.getLogger()
    muted = [
        (h, h.level)
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    for handler, _ in muted:
        handler.setLevel(logging.CRITICAL + 1)

    previous_factory = None
    try:
        import structlog

        previous_factory = structlog.get_config().get("logger_factory")
        structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())
    except Exception:  # structlog is optional here; never block the UI on it
        previous_factory = None
    try:
        yield
    finally:
        for handler, level in muted:
            handler.setLevel(level)
        if previous_factory is not None:
            with contextlib.suppress(Exception):
                import structlog

                structlog.configure(logger_factory=previous_factory)


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


class _LiveSizedConsole(Console):
    """A Rich ``Console`` whose ``.size`` always reflects this app's *current*
    width/height instead of a cached snapshot.

    Rich's own ``Console.size`` getter only auto-detects from the real
    terminal (``os.get_terminal_size()``) as long as nothing has ever
    assigned to ``.size`` — the instant anything does (even once), Rich
    switches to returning that fixed value forever, since its setter just
    pins ``self._width``/``self._height`` and the getter's fast path returns
    them directly without re-checking the terminal. A previous version of
    this class synced ``console.size = (...)`` on every write to work around
    staleness, but that assignment is itself what disables live detection,
    and syncing happens after Rich has already wrapped the content that
    triggered the write — so it only ever corrected the *next* print, not
    the one in flight, and did so via the very mechanism that broke live
    detection.  Overriding the property instead means every render call
    (panels, tables, markdown, plain prints) picks up
    :meth:`FullscreenREPLUI._width`/``_height`` fresh, with no lag and no
    pinning — the same live-width pattern already used by ``_render_home``
    and ``_render_status``.
    """

    def __init__(self, ui: FullscreenREPLUI, **kwargs: Any) -> None:
        self._sync_ui = ui
        super().__init__(**kwargs)

    @property
    def size(self) -> ConsoleDimensions:
        return ConsoleDimensions(self._sync_ui._width(), self._sync_ui._height())

    @size.setter
    def size(self, new_size: tuple[int, int]) -> None:
        # Rich's own __init__ assigns to `.size` when constructed with
        # explicit width=/height= kwargs — accepted as a no-op since this
        # class is never constructed that way (see `FullscreenREPLUI.__init__`)
        # and live detection must stay in control either way.
        pass


class ToolCardHandle:
    """Live handle to one tool-activity line in the transcript.

    Holds its `_Line` by object reference — never by index — so `_trim()`
    dropping leading lines can't misdirect an update: a trimmed-away card
    simply stops rendering, and further ticks/resolves are harmless no-ops.
    """

    def __init__(self, ui: FullscreenREPLUI, line: _Line, verb: str, target: str) -> None:
        self._ui = ui
        self._line = line
        self._verb = verb
        self._target = target
        self._started = time.perf_counter()
        self._task: asyncio.Task | None = None
        self._done = False

    def _title_fragments(
        self, bullet: str, bullet_style: str, elapsed_s: float | None = None
    ) -> list[tuple[str, str]]:
        frags = [
            (bullet_style, f"{bullet} "),
            ("class:conversation.tool.name", self._verb),
        ]
        if self._target:
            frags.append(("class:conversation.tool.arg", f"({self._target})"))
        # Elapsed time only earns a slot once the tool stops feeling instant.
        if elapsed_s is not None and elapsed_s >= 2.0:
            frags.append(("class:conversation.tool.elapsed", f" · {elapsed_s:.1f}s"))
        return frags

    def _set_line(self, frags: list[tuple[str, str]]) -> None:
        self._line.fragments = frags
        self._line.text = "".join(t for _s, t in frags)

    def tick(self, frame: str, elapsed_s: float) -> None:
        if self._done:
            return
        self._set_line(self._title_fragments(frame, "class:conversation.tool.spinner", elapsed_s))

    def resolve(
        self,
        summary: list[tuple[str, str]] | None = None,
        *,
        error: bool = False,
        bullet_style: str | None = None,
    ) -> None:
        if self._done:
            return
        self._done = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        style = bullet_style or (
            "class:conversation.tool.err" if error else "class:conversation.tool.ok"
        )
        self._set_line(self._title_fragments("●", style))
        if summary:
            self._ui.append_fragment_lines([[("class:conversation.tool.result", "  ⎿ "), *summary]])
        try:
            self._ui._live_cards.remove(self)
        except ValueError:
            pass
        self._ui.invalidate()

    def cancel(self, note: str = "Interrupted") -> None:
        self.resolve(
            [("class:conversation.tool.warn", note)],
            bullet_style="class:conversation.tool.warn",
        )


def _build_floats(
    command_palette: Any | None,
    model_switcher: Any | None = None,
    inline_flow: Any | None = None,
    model_palette: Any | None = None,
    theme_palette: Any | None = None,
) -> list[Float]:
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
    if model_palette is not None:
        # Same rectangle as the command palette, for the same reason the inline
        # flow uses it: typing "/model" should read as the palette staying put
        # and switching from commands to models. The command palette suppresses
        # itself while this one is active, so the two never render stacked.
        floats.append(
            Float(
                content=model_palette.container(),
                left=2,
                right=2,
                top=1,
                height=18,
                allow_cover_cursor=True,
                z_index=21,
            )
        )
    if theme_palette is not None:
        # Same rectangle again, for the same reason: "/theme" reads as the
        # palette staying put and switching from commands to themes. The
        # command palette suppresses itself while this one is active.
        floats.append(
            Float(
                content=theme_palette.container(),
                left=2,
                right=2,
                top=1,
                height=18,
                allow_cover_cursor=True,
                z_index=21,
            )
        )
    if inline_flow is not None:
        # Deliberately the palette's exact geometry: a slash command that opens
        # a flow (`/connect`) should read as the palette staying put and
        # changing what it lists, not as one panel closing and another opening
        # somewhere else. The two are never active at once — the palette
        # suppresses itself while a flow owns the prompt box.
        floats.append(
            Float(
                content=inline_flow.container(),
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


class _ScrollableConversationWindow(Window):
    """The conversation `Window`, with mouse-wheel ticks routed through
    explicit callbacks instead of prompt_toolkit's default scroll handler.

    `Window._mouse_handler`'s default SCROLL_UP/SCROLL_DOWN behavior mutates
    `self.vertical_scroll` directly, which fights `_scroll_anchor` — the
    application-level bookkeeping that also drives PageUp/PageDown/Ctrl+Home
    (see `FullscreenREPLUI._sync_conversation_scroll`). Overriding
    `_mouse_handler` here — the same hook `Window` itself uses internally —
    keeps every scroll input path (keyboard and wheel alike) going through
    one piece of state instead of two that can disagree.

    Mouse events only ever reach a `Window` whose screen region the pointer
    is actually over (`mouse_handlers.set_mouse_handler_for_range` in
    prompt_toolkit's container rendering, keyed by each window's own
    write-position box), so wheel ticks here are already naturally isolated
    from the separate composer `Window` below it — no shared/global key
    binding is involved.
    """

    def __init__(self, *args: Any, on_scroll_up: Any, on_scroll_down: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_scroll_up = on_scroll_up
        self._on_scroll_down = on_scroll_down

    def _mouse_handler(self, mouse_event: Any) -> Any:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._on_scroll_up()
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll_down()
            return None
        return super()._mouse_handler(mouse_event)


class FullscreenREPLUI:
    """Owns the alternate-screen UI, prompt buffer, status line, and transcript."""

    def _build_style(self) -> dict[str, str]:
        """The complete style sheet, interpolated from the *current* theme.

        Called once at construction and again by :meth:`restyle` after a theme
        change. Every value below reads ``design.*`` at call time, so this
        function is the reason a theme swap can repaint the transcript, prompt,
        and chrome without rebuilding the Application.
        """
        style = dict(self._style_fragments)
        if self._style_provider is not None:
            try:
                style.update(self._style_provider())
            except Exception:
                # A broken provider must never take the UI down; the frozen
                # fragments captured at construction remain a usable sheet.
                _log.debug("Style provider failed; keeping previous fragments", exc_info=True)
        style.update(
            {
                "": f"bg:{design.BACKGROUND} {design.WHITE}",
                "conversation": f"bg:{design.BACKGROUND} {design.WHITE}",
                "conversation.dim": f"bg:{design.BACKGROUND} {design.SECONDARY}",
                "conversation.user": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "conversation.user.marker": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "conversation.user.text": f"bg:{design.BACKGROUND} {design.WHITE}",
                "conversation.assistant": f"bg:{design.BACKGROUND} {design.WHITE}",
                "conversation.assistant.diamond": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "conversation.assistant.label": f"bg:{design.BACKGROUND} {design.GRAD_MID} bold",
                "conversation.system": f"bg:{design.BACKGROUND} {design.SECONDARY}",
                "conversation.thinking": f"bg:{design.BACKGROUND} {design.SECONDARY} italic",
                "conversation.spinner": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                # Tool-activity cards (agentic loop): ● Verb(target) · 1.2s
                "conversation.tool.spinner": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "conversation.tool.name": f"bg:{design.BACKGROUND} {design.WHITE} bold",
                "conversation.tool.arg": f"bg:{design.BACKGROUND} {design.SECONDARY}",
                "conversation.tool.elapsed": f"bg:{design.BACKGROUND} {design.FAINT}",
                "conversation.tool.ok": f"bg:{design.BACKGROUND} {design.OK} bold",
                "conversation.tool.err": f"bg:{design.BACKGROUND} {design.DANGER} bold",
                "conversation.tool.warn": f"bg:{design.BACKGROUND} {design.WARN} bold",
                "conversation.tool.result": f"bg:{design.BACKGROUND} {design.MUTED}",
                # Inline diff blocks nested under tool cards
                "diff.add": f"bg:{design.BACKGROUND} {design.OK}",
                "diff.del": f"bg:{design.BACKGROUND} {design.DANGER}",
                "diff.hunk": f"bg:{design.BACKGROUND} {design.ACCENT_SOFT}",
                "diff.meta": f"bg:{design.BACKGROUND} {design.FAINT}",
                **home_styles(),
                # Hide separators for minimal look
                "separator": f"bg:{design.BACKGROUND} {design.BACKGROUND}",
                "prompt": f"bg:{design.BACKGROUND} {design.WHITE}",
                "prompt.prefix": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
                "prompt.border": f"bg:{design.BACKGROUND} {design.FAINT}",
                "prompt.hint": f"bg:{design.BACKGROUND} {design.FAINT} italic",
            }
        )
        return style

    def restyle(self) -> None:
        """Rebuild and reinstall the style sheet from the current theme.

        The Application, layout, buffers, and transcript are all untouched —
        only ``app.style`` is replaced — so a theme change costs one repaint
        and never disturbs scroll position, in-flight generation, or the text
        being typed. No restart, and nothing to re-enter.

        Safe to call before the app exists (during startup); it becomes a
        no-op, and the constructor will pick the theme up anyway.
        """
        if self._app is None:
            return
        self._app.style = Style.from_dict(design.themed_style(self._build_style()))
        self._app.invalidate()

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
        style_provider: Any | None = None,
        on_status_render: Any | None = None,
        command_palette: Any | None = None,
        model_palette: Any | None = None,
        theme_palette: Any | None = None,
        model_switcher: Any | None = None,
        inline_flow: Any | None = None,
        home_provider: Any | None = None,
        input: Any | None = None,
        output: Any | None = None,
        max_content_width: int | None = None,
        composer_min_lines: int | None = None,
        composer_max_lines: int | None = None,
    ) -> None:
        self._status_state = status_state
        # `None` (the default) means "no cap" — content fills the full
        # terminal width. Non-positive configured values are treated the
        # same way rather than producing a zero-or-negative Dimension.
        self._max_content_width = (
            max_content_width if max_content_width and max_content_width > 0 else None
        )
        # Composer height bounds — see `_prompt_window_height`. Defensively
        # clamped rather than validated at the config layer (mirrors how
        # `max_content_width` above is handled): a misconfigured max below
        # min just widens to match min instead of producing an inverted
        # range no `Dimension` could satisfy.
        self._prompt_min_lines = (
            composer_min_lines
            if composer_min_lines and composer_min_lines > 0
            else _PROMPT_MIN_LINES_DEFAULT
        )
        self._prompt_max_lines = max(
            self._prompt_min_lines,
            composer_max_lines
            if composer_max_lines and composer_max_lines > 0
            else _PROMPT_MAX_LINES_DEFAULT,
        )
        # Set once content reaches `_prompt_max_lines`; keeps the composer's
        # outer height pinned at the max for the rest of the current draft
        # even if the user then deletes text back down, so the box never
        # jumps around mid-edit. Clears when the buffer goes empty (message
        # sent, or manually cleared) — see `_prompt_window_height`.
        self._prompt_frozen_at_max = False
        self._inline_flow = inline_flow
        self._theme_palette = theme_palette
        self._on_status_render = on_status_render
        # Callable returning a fresh HomeState; rendered while the transcript
        # is empty. None falls back to a minimal wordmark line.
        self._home_provider = home_provider
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._lines: list[_Line] = []
        # True once the conversation has produced its first line, ever.
        # Only `clear()` resets this alongside emptying `self._lines` — kept
        # as an explicit flag (rather than just checking `self._lines`) so
        # the home-screen-vs-blank distinction reads the same way at every
        # call site that inspects it.
        self._history_started = False
        # None = pinned to the bottom (live-follow, the only behavior before
        # in-app scrolling existed). An int is an *absolute* index into
        # `self._lines` for the line currently anchored in the viewport —
        # absolute rather than "distance from the bottom" so that appending
        # new lines while scrolled up never has to touch this value; only
        # `_trim()` evicting from the front needs to shift it.
        self._scroll_anchor: int | None = None
        self._stream_start: int | None = None
        self._stream_text = ""
        # Full text of the most recently completed assistant turn — captured
        # in `finish_assistant()` before `_stream_text` resets, so Ctrl+Y /
        # Ctrl+G can copy it after the streaming state that produced it is
        # long gone. A multi-turn tool-loop response overwrites this at each
        # `finish_assistant()` call, so it always ends up holding the final
        # answer segment rather than intermediate tool-narration text.
        self._last_response_text = ""
        self._last_stream_render: float = 0.0
        self._running = False
        self._app: Application[None] | None = None
        self._busy = False
        self._live_cards: list[ToolCardHandle] = []
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

        # While an InlineFlow step owns the prompt box, its text is a palette
        # filter or an API key — not a prompt. Everything that assumes the
        # latter has to stand down: completion popups, syntax validation, and
        # above all history auto-suggest, which would otherwise ghost a
        # previous command behind a key the user is pasting.
        flow_idle = ~Condition(inline_flow.is_active) if inline_flow else True

        self.buffer = Buffer(
            history=history,
            completer=completer,
            auto_suggest=ConditionalAutoSuggest(AutoSuggestFromHistory(), flow_idle),
            validator=validator,
            complete_while_typing=flow_idle,
            validate_while_typing=flow_idle,
            multiline=True,
            accept_handler=_on_accept,
        )
        if inline_flow is not None:
            inline_flow.bind(self.buffer, self.invalidate)

        self.console = _LiveSizedConsole(
            self,
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

        # Kept so `restyle()` can rebuild the whole sheet after a theme change.
        # A *callable* provider is preferred over the plain dict: the dict was
        # interpolated from `design.*` by the caller and is therefore frozen at
        # whichever theme was active then, which is exactly what made live
        # theme switching impossible before.
        self._style_provider = style_provider
        self._style_fragments = dict(style_fragments)
        style = self._build_style()

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
            # Ordering note: two eager bindings on the same key resolve to the
            # last one registered, and this one is registered after
            # InlineFlow's. So the flow is deferred to here, explicitly, rather
            # than by relying on who called kb.add() first.
            if inline_flow is not None and inline_flow.is_active():
                inline_flow.cancel()
                event.app.invalidate()
                return
            # A running turn takes priority over clearing the input buffer.
            # Previously any text in the buffer swallowed the interrupt, so a
            # user who typed while the model was generating could not cancel it
            # — Ctrl+C only wiped what they had typed.
            if self._busy:
                should_exit = bool(on_interrupt(event))
                if should_exit:
                    self._queue.put_nowait("/exit")
                return
            if event.current_buffer.text:
                event.current_buffer.reset(Document(""))
            else:
                should_exit = bool(on_interrupt(event))
                if should_exit:
                    self._queue.put_nowait("/exit")

        # In-app scrolling of the conversation transcript — the alternate
        # screen buffer has no native OS scrollback, so this is the only way
        # to review history. Bound globally (not filtered to a focused
        # window) since the conversation pane is never itself focusable;
        # `eager=True` so these win over any default multiline-buffer
        # binding on the same keys.
        @kb.add("pageup", eager=True)
        def _(event) -> None:
            self.scroll_page_up()

        @kb.add("pagedown", eager=True)
        def _(event) -> None:
            self.scroll_page_down()

        # Mouse-wheel scrolling is *not* bound here as a global key: real
        # wheel ticks arrive as positional mouse events (SGR mouse mode on
        # most terminals, `win32-input-mode`/console mouse events here on
        # Windows) and prompt_toolkit already routes those to whichever
        # `Window` the pointer is actually over — the composer below scrolls
        # itself via its own `BufferControl`, and the conversation window's
        # wheel handling is wired directly on `_ScrollableConversationWindow`
        # (see its class docstring) so it shares `_scroll_anchor` bookkeeping
        # with PageUp/PageDown instead of fighting it. A global eager
        # binding here would re-couple the two panes and break that
        # per-region isolation.
        @kb.add("c-home", eager=True)
        def _(event) -> None:
            self.scroll_to_top()

        @kb.add("c-end", eager=True)
        def _(event) -> None:
            self.scroll_to_bottom()

        # Clipboard actions via OSC 52 — works over SSH/tmux without any
        # platform clipboard tool, since the terminal itself does the copy.
        @kb.add("c-y", eager=True)
        def _(event) -> None:
            self.copy_last_response()

        @kb.add("c-g", eager=True)
        def _(event) -> None:
            self.copy_last_code_block()

        # Named (not built anonymously in the HSplit list below) so scroll
        # methods can nudge `.vertical_scroll` directly — see
        # `_sync_conversation_scroll`. `on_scroll_up`/`on_scroll_down` route
        # mouse-wheel ticks over this pane through the same `_scroll_anchor`
        # path as PageUp/PageDown — see `_ScrollableConversationWindow`.
        self._conversation_window = _ScrollableConversationWindow(
            FormattedTextControl(self._render_conversation),
            style="class:conversation",
            wrap_lines=True,
            always_hide_cursor=True,
            on_scroll_up=lambda: self.scroll_up(3),
            on_scroll_down=lambda: self.scroll_down(3),
        )

        # Named for the same reason as `_conversation_window` — tests read
        # `render_info.window_height` off of it directly to assert the
        # grow/freeze contract in `_prompt_window_height`.
        self._prompt_window = Window(
            BufferControl(
                buffer=self.buffer,
                input_processors=[
                    # The API-key step types into this same box, so masking
                    # has to be a property of the box rather than of a
                    # separate password field.
                    ConditionalProcessor(
                        PasswordProcessor(),
                        Condition(inline_flow.is_masked)
                        if inline_flow
                        else Condition(lambda: False),
                    )
                ],
            ),
            height=self._prompt_window_height,
            # Without this, `HSplit`'s own layout algorithm — not this
            # class's height logic — is what actually determines the
            # rendered height: after satisfying every child's *preferred*
            # size, it round-robins any terminal rows still left over up to
            # each child's *max* (`HSplit._divide_heights`, prompt_toolkit's
            # `containers.py`), and it doesn't stop at "preferred" just
            # because we computed one carefully. `dont_extend_height` makes
            # `Window.preferred_height()` report `max == preferred`, so that
            # redistribution has nothing left to hand this window — the
            # conversation pane above absorbs all of it instead, which is
            # what "fixed composer, elastic transcript" requires.
            dont_extend_height=True,
            wrap_lines=True,
            style="class:prompt",
            get_line_prefix=self._prompt_line_prefix,
        )

        content = FloatContainer(
            content=HSplit(
                [
                    self._conversation_window,
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
                    self._prompt_window,
                    Window(
                        FormattedTextControl(self._render_prompt_bottom_border),
                        height=1,
                        always_hide_cursor=True,
                    ),
                ],
                # `self._max_content_width` is `None` by default, so `content`
                # has no explicit width and simply fills whatever the parent
                # `VSplit` gives it — the whole terminal width. When a cap
                # *is* configured, this pins `content` to it and `align=LEFT`
                # below sends the leftover width into a trailing gutter
                # instead of the chrome, letterboxing it against the left
                # edge. `_width()` mirrors this same cap so border-drawing
                # and the home screen match what actually gets allocated.
                width=(
                    Dimension(max=self._max_content_width, preferred=self._max_content_width)
                    if self._max_content_width is not None
                    else None
                ),
            ),
            floats=_build_floats(
                command_palette,
                model_switcher,
                inline_flow,
                model_palette=model_palette,
                theme_palette=theme_palette,
            ),
        )

        root_style = f"bg:{design.BACKGROUND}" if design.color_enabled() else ""
        root = VSplit([content], align=HorizontalAlign.LEFT, style=root_style)

        self._app = Application(
            layout=Layout(root, focused_element=self.buffer),
            key_bindings=kb,
            style=Style.from_dict(design.themed_style(style)),
            # The alternate screen buffer: Velune owns the terminal the way
            # vim/k9s/btop do — the shell prompt is fully hidden while this
            # app runs, and `erase_when_done=True` below makes exit restore
            # the original primary-buffer content exactly (prompt_toolkit
            # does this via a real second console buffer on Windows, ANSI
            # 1049h/l elsewhere). The tradeoff is that this buffer has no
            # native OS scrollback, so `self._lines` now stays resident for
            # the whole session instead of being flushed out line-by-line —
            # scrolling through history is handled in-app (see
            # `scroll_up`/`scroll_page_up`/etc. and `_scroll_anchor` below)
            # rather than via the terminal's own scrollback buffer.
            full_screen=True,
            # Without this, prompt_toolkit's own shutdown path
            # (`Application._redraw(render_as_done=True)`, run unconditionally
            # in a `finally` around `run_async`'s wait — covers `.exit()`,
            # `/exit`, and an interrupt/cancellation alike) repaints the live
            # chrome one last time in "done" state before leaving the
            # alternate screen. `erase_when_done=True` makes that same
            # finally-block erase instead of repaint — belt-and-suspenders,
            # since leaving the alternate screen already discards whatever
            # was drawn there, but this avoids a one-frame flash of the full
            # chrome immediately before the screen swap.
            erase_when_done=True,
            # Needed for `Keys.ScrollUp`/`Keys.ScrollDown` (mouse wheel) to
            # reach the key bindings below instead of being silently dropped.
            mouse_support=True,
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
            with _muted_console_logging():
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
        self._history_started = True
        self._append_gap()
        # The user's turn echoes in the same voice as the prompt box: the
        # first line carries the ❯ glyph, continuations align under it.
        lines = (text or "").splitlines() or [""]
        first = lines[0].rstrip()
        self._lines.append(
            _Line(
                f"❯ {first}",
                "class:conversation",
                fragments=[
                    ("class:conversation.user.marker", "❯ "),
                    ("class:conversation.user.text", first),
                ],
            )
        )
        for raw in lines[1:]:
            self._lines.append(_Line(f"  {raw.rstrip()}", "class:conversation.user.text"))
        self._trim()

    def append_console_line(self, line: str) -> None:
        if not line and (not self._lines or not self._lines[-1].text):
            return
        self._history_started = True
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
        self._history_started = True
        self._append_wrapped(text, "class:conversation.system")
        self._trim()
        self.invalidate()

    def begin_assistant(
        self,
        text: str = "",
        *,
        cycle: bool = True,
        show_label: bool = True,
    ) -> None:
        """Open an assistant block: label line + animated thinking spinner.

        ``text`` seeds the spinner verb (empty → the shuffled verb pool);
        ``cycle=False`` pins the verb (only the spinner glyph animates) — used
        for the between-tool-batches "Continuing…" state; ``show_label=False``
        skips the ``◆ Velune`` header so continuation turns of one response
        don't repeat it.
        """
        self._history_started = True
        self._append_gap()
        if show_label:
            self._lines.append(
                _Line(
                    "◆ Velune",
                    "class:conversation.assistant.label",
                    fragments=[
                        ("class:conversation.assistant.diamond", "◆ "),
                        ("class:conversation.assistant.label", "Velune"),
                    ],
                )
            )
        self._stream_start = len(self._lines)
        self._thinking_idx = 0
        self._stream_text = text or self._thinking_words[0]
        self._set_thinking_line(_SPINNER_FRAMES[0], self._stream_text)
        self.set_busy_hint(True)
        self.invalidate()

        if design.reduced_motion_enabled():
            # Static frame only — no cycling glyph/verb, no ticking task.
            self._thinking_task = None
            return

        async def _thinking_anim():
            tick = 0
            while True:
                await asyncio.sleep(_THINKING_TICK_S)
                tick += 1
                if cycle and tick % _THINKING_WORD_EVERY == 0:
                    self._thinking_idx = (self._thinking_idx + 1) % len(self._thinking_words)
                    self._stream_text = self._thinking_words[self._thinking_idx]
                self._set_thinking_line(
                    _SPINNER_FRAMES[tick % len(_SPINNER_FRAMES)], self._stream_text
                )
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
            self.begin_assistant()
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
        if self._stream_text:
            self._last_response_text = self._stream_text
        self._stream_start = None
        self._stream_text = ""
        self.set_busy_hint(False)
        self.invalidate()

    def set_busy_hint(self, active: bool) -> None:
        """Swap the prompt's bottom-border hint while a turn is in flight."""
        if self._busy != active:
            self._busy = active
            self.invalidate()

    # ── Clipboard actions (OSC 52) ────────────────────────────────────

    def copy_last_response(self) -> bool:
        """Copy the last completed assistant response to the system clipboard."""
        if not self._last_response_text:
            self.console.print("[dim]Nothing to copy yet.[/dim]")
            return False
        ok = copy_to_clipboard(self._app, self._last_response_text)
        self.console.print(
            "[dim]Response copied to clipboard.[/dim]"
            if ok
            else "[dim]Clipboard copy failed — terminal may not support OSC 52.[/dim]"
        )
        return ok

    def copy_last_code_block(self) -> bool:
        """Copy the last fenced code block in the last assistant response."""
        block = extract_last_code_block(self._last_response_text)
        if not block:
            self.console.print("[dim]No code block found in the last response.[/dim]")
            return False
        ok = copy_to_clipboard(self._app, block)
        self.console.print(
            "[dim]Code block copied to clipboard.[/dim]"
            if ok
            else "[dim]Clipboard copy failed — terminal may not support OSC 52.[/dim]"
        )
        return ok

    # ── Tool-activity cards ──────────────────────────────────────────

    def add_tool_card(self, verb: str, target: str) -> ToolCardHandle:
        """Append a live `● Verb(target)` card with its own spinner task.

        The loop's event order guarantees the assistant stream is closed
        before any tool starts (`turn_end` precedes `tool_start`), but guard
        anyway: a live thinking line wipes everything after `_stream_start`
        on every tick (`_set_thinking_line` slices `_lines[start:]`), which
        would silently delete the card.
        """
        self._history_started = True
        if self._stream_start is not None:
            self.finish_assistant()
        self.set_busy_hint(True)  # the turn is still in flight while tools run
        self._append_gap()
        line = _Line("", "class:conversation")
        self._lines.append(line)
        handle = ToolCardHandle(self, line, verb, target)
        handle.tick(_SPINNER_FRAMES[0], 0.0)
        self._live_cards.append(handle)
        self._trim()
        self.invalidate()

        if design.reduced_motion_enabled():
            # Static frame only — no cycling glyph, no ticking task.
            return handle

        async def _spin() -> None:
            tick = 0
            while True:
                await asyncio.sleep(_THINKING_TICK_S)
                tick += 1
                handle.tick(
                    _SPINNER_FRAMES[tick % len(_SPINNER_FRAMES)],
                    time.perf_counter() - handle._started,
                )
                self.invalidate()

        from velune.core.task_registry import track

        handle._task = track(asyncio.create_task(_spin(), name="velune.fullscreen_toolcard"))
        return handle

    def append_fragment_lines(self, lines: list[list[tuple[str, str]]]) -> None:
        """Append pre-built prompt_toolkit fragment lines (result rows, diffs)."""
        self._history_started = True
        for frags in lines:
            plain = "".join(t for _s, t in frags)
            self._lines.append(_Line(plain, "class:conversation.system", fragments=frags))
        self._trim()
        self.invalidate()

    def cancel_live_cards(self, note: str = "Interrupted") -> None:
        """Resolve any still-running cards (interrupt/teardown safety net)."""
        for handle in list(self._live_cards):
            handle.cancel(note)

    # ── In-app scrolling ─────────────────────────────────────────────
    #
    # The alternate screen buffer has no native terminal scrollback, so
    # reviewing history is handled entirely inside this class via
    # `_scroll_anchor` (see its docstring in `__init__`) — moving *which*
    # line carries the `[SetCursorPosition]` marker in `_render_conversation`
    # is what moves the viewport (`Window._scroll_when_linewrapping` scrolls
    # to keep that marker visible). That auto-scroll logic is stateful,
    # though: it only forces `vertical_scroll` to change when the marker
    # falls outside the *previous* visible range, so a small move (a
    # mouse-wheel tick) can otherwise silently do nothing if it lands back
    # inside the range that was already on screen. `_sync_conversation_scroll`
    # sidesteps that by setting `vertical_scroll` directly whenever the
    # anchor is concrete, which always renders the anchor line as the
    # topmost visible row — deterministic regardless of move size.

    def _sync_conversation_scroll(self) -> None:
        if self._scroll_anchor is not None:
            self._conversation_window.vertical_scroll = self._scroll_anchor
        # `None` (resuming live-follow) is left alone: the marker moving
        # back to the last line always violates the auto-scroll's own
        # "keep the cursor from going below the bottom" bound (unless we
        # were already at the bottom), which reliably snaps the viewport
        # back down on its own.

    def _page_size(self) -> int:
        return max(3, self._height() - 2)

    def scroll_up(self, n: int = 1) -> None:
        current = self._scroll_anchor if self._scroll_anchor is not None else len(self._lines) - 1
        self._scroll_anchor = max(0, current - n)
        self._sync_conversation_scroll()
        self.invalidate()

    def scroll_down(self, n: int = 1) -> None:
        if self._scroll_anchor is None:
            return
        new_anchor = self._scroll_anchor + n
        if new_anchor >= len(self._lines) - 1:
            self._scroll_anchor = None  # caught up — resume live-follow
        else:
            self._scroll_anchor = new_anchor
        self._sync_conversation_scroll()
        self.invalidate()

    def scroll_page_up(self) -> None:
        self.scroll_up(self._page_size())

    def scroll_page_down(self) -> None:
        self.scroll_down(self._page_size())

    def scroll_to_top(self) -> None:
        self._scroll_anchor = 0
        self._sync_conversation_scroll()
        self.invalidate()

    def scroll_to_bottom(self) -> None:
        self._scroll_anchor = None
        self._sync_conversation_scroll()
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
        """Reset the live view for `/clear`.

        Unlike a real shell's `clear` (which only clears the visible screen,
        leaving scrollback intact), this genuinely erases the conversation:
        the alternate screen buffer has no scrollback for anything to
        survive in. The home screen reappears since, as far as this view is
        concerned, the conversation is starting over.
        """
        self._lines.clear()
        self._stream_start = None
        self._stream_text = ""
        self._last_response_text = ""
        self._scroll_anchor = None
        self._history_started = False
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
        self._lines.append(
            _Line(f"{spinner} {word}", "class:conversation.thinking", fragments=frags)
        )
        self._trim()

    def _append_gap(self) -> None:
        if self._lines and self._lines[-1].text:
            self._lines.append(_Line(""))

    def _append_wrapped(self, text: str, style: str) -> None:
        for raw in (text or "").splitlines() or [""]:
            self._lines.append(_Line(raw.rstrip(), style))

    def _trim(self) -> None:
        """Drop the oldest lines once the transcript exceeds the cap.

        The alternate screen buffer has no scrollback, so unlike a real
        terminal this cap is a hard memory bound, not just a viewport
        clip — content evicted here is genuinely gone, the same tradeoff
        `k9s`/`btop`-style TUIs make with their own internal buffers.
        `_stream_start` and `_scroll_anchor` are absolute indices into
        `self._lines`, so both need to shift down by however much was
        evicted from the front.
        """
        if len(self._lines) <= _MAX_TRANSCRIPT_LINES:
            return
        overflow = len(self._lines) - _MAX_TRANSCRIPT_LINES
        self._lines = self._lines[overflow:]
        if self._stream_start is not None:
            self._stream_start = max(0, self._stream_start - overflow)
        if self._scroll_anchor is not None:
            self._scroll_anchor = max(0, self._scroll_anchor - overflow)

    def _render_status(self) -> AnyFormattedText:
        if self._on_status_render is not None:
            self._on_status_render()
        return render_status_bar(self._status_state, self._width())

    def _render_prompt_top_border(self) -> AnyFormattedText:
        width = self._width()
        return FormattedText([("class:prompt.border", "╭" + "─" * max(1, width - 2) + "╮")])

    def _render_prompt_bottom_border(self) -> AnyFormattedText:
        width = self._width()
        if self._inline_flow is not None and self._inline_flow.is_active():
            # The flow owns the box; the usual "Enter send / @@ files" hints
            # describe keys that do something else entirely right now.
            hint = self._inline_flow.hint()
        elif self._busy:
            hint = "Ctrl+C interrupt"
        elif self._scroll_anchor is not None:
            hint = "↑ scrolled  ·  Ctrl+End for latest"
        else:
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

    def _prompt_content_lines(self) -> int:
        """Estimate how many visual rows the buffer's current text wraps to.

        Cheap on purpose: a single pass over `text.split("\n")` with integer
        division, no lexer/processor pass and no wide-character awareness —
        same rigor level as the border-width math elsewhere in this class.
        It only has to be right around the `_prompt_min_lines`/
        `_prompt_max_lines` boundary; sub-pixel accuracy at 5,000 lines
        doesn't change the answer (still "past max, freeze").
        """
        text = self.buffer.text
        if not text:
            return 1
        avail = max(1, self._width() - _PROMPT_LINE_PREFIX_WIDTH)
        total = 0
        for raw_line in text.split("\n"):
            total += max(1, -(-len(raw_line) // avail))  # ceil(len / avail)
        return max(1, total)

    def _prompt_window_height(self) -> Dimension:
        """Content-driven composer height: idle at `_prompt_min_lines`,
        grows with typed/pasted content, and permanently freezes at
        `_prompt_max_lines` for the rest of the current draft once reached
        — see `_prompt_frozen_at_max`'s docstring in `__init__`. Beyond the
        max, overflow content stays in the buffer and scrolls inside the
        fixed viewport via prompt_toolkit's own cursor-follow scrolling
        (`BufferControl`'s default behavior); the outer `Dimension` returned
        here never changes shape once frozen, which is what keeps every
        other row in the layout (conversation, status bar, borders) from
        shifting while a long prompt is being edited.

        Invoked fresh on every render pass (`Window.preferred_height` calls
        `to_dimension(self.height)`, and `self.height` is this bound method)
        — so a huge paste only ever costs one `_prompt_content_lines()` call
        before `_prompt_frozen_at_max` short-circuits every render after.
        """
        if not self.buffer.text:
            self._prompt_frozen_at_max = False
            preferred = self._prompt_min_lines
        elif self._prompt_frozen_at_max:
            preferred = self._prompt_max_lines
        else:
            lines = self._prompt_content_lines()
            if lines >= self._prompt_max_lines:
                self._prompt_frozen_at_max = True
                preferred = self._prompt_max_lines
            else:
                preferred = max(self._prompt_min_lines, lines)
        return Dimension(
            min=self._prompt_min_lines, max=self._prompt_max_lines, preferred=preferred
        )

    def _prompt_line_prefix(self, line_number: int, wrap_count: int) -> AnyFormattedText:
        # First visual line gets the prompt glyph; wrapped/continuation
        # lines get matching blank padding so multi-line input stays aligned
        # inside the border instead of flush against the left edge.
        if line_number == 0 and wrap_count == 0:
            label = self._inline_flow.prompt_label() if self._inline_flow is not None else ""
            if label:
                # Naming the step in the caret is what makes "the same box now
                # wants your API key" legible without a separate field.
                return FormattedText(
                    [
                        ("class:prompt.border", "│ "),
                        ("class:prompt.hint", f"{label} "),
                        ("class:prompt.arrow", "❯ "),
                    ]
                )
            return FormattedText([("class:prompt.border", "│ "), ("class:prompt.arrow", "❯ ")])
        return FormattedText([("class:prompt.border", "│ "), ("", "  ")])

    def _render_conversation(self) -> AnyFormattedText:
        if not self._lines:
            # Once flushed, finished turns live in real scrollback, not
            # here — an empty live area no longer means "nothing was ever
            # said," only "nothing is currently in flight."
            return FormattedText([]) if self._history_started else self._render_home()

        # `self._lines` holds the *entire* transcript (the alternate screen
        # buffer has no native scrollback for anything trimmed out of it —
        # see the `_scroll_anchor` docstring in `__init__`). No clipping
        # here; the viewport is controlled purely by where the
        # `[SetCursorPosition]` marker below lands.
        last_idx = len(self._lines) - 1
        anchor = self._scroll_anchor if self._scroll_anchor is not None else last_idx
        anchor = max(0, min(anchor, last_idx))
        fragments: list[tuple[str, str]] = []
        for idx, line in enumerate(self._lines):
            if idx == anchor:
                # `Window`'s "keep the cursor visible" scroll logic scrolls
                # to keep whichever line carries this zero-width marker in
                # view (nothing in this control ever sets a real cursor,
                # since `always_hide_cursor=True`). Anchoring the last line
                # (the default, live-follow mode) keeps new output visible
                # as it streams in; `scroll_up`/`scroll_page_up`/etc. move
                # the anchor earlier so the user can review history without
                # being yanked back down by new content arriving below it.
                fragments.append(("[SetCursorPosition]", ""))
            if line.fragments is not None:
                fragments.extend(line.fragments)
                sep_style = ""
            else:
                fragments.append((line.style, line.text))
                sep_style = line.style
            if idx < last_idx:
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
            # Mirrors the `self._max_content_width` cap on the layout's content
            # column (`__init__`, the `content = FloatContainer(...)` / `root
            # = VSplit(...)` pair) — this is what the border, home screen,
            # and markdown rendering actually get allocated. `None` means no
            # cap: the full terminal width, same as what `content` fills.
            columns = max(20, self._app.output.get_size().columns)
            if self._max_content_width is None:
                return columns
            return min(self._max_content_width, columns)
        except Exception:
            return 80

    def _height(self) -> int:
        if self._app is None:
            return 24
        try:
            return max(10, self._app.output.get_size().rows)
        except Exception:
            return 24
