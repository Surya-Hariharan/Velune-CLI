"""Velune CLI design system — shared terminal UI components.

Single source of truth for all terminal rendering in Velune.  Every screen,
command, and interactive view imports from here.  No panel or styled-text
construction happens outside this module.

Component philosophy (matching gh, uv, docker, Claude Code):
  Minimalist   — structure and whitespace carry meaning, not decoration
  Semantic     — color signals state (ok/warn/error), not aesthetics
  Keyboard-first — shortcuts always visible, focus states always clear
  Consistent   — same spacing, borders, and typography on every screen

Component catalog
─────────────────
  Inline (single-line, no border):
    notification(msg, kind)     info / success / warning / error line
    success(msg)                green ✓ line
    warning(msg)                amber ⚠ line
    error(msg)                  red ✗ line
    info(msg)                   muted · line

  Structural (bordered blocks):
    header(title, subtitle)     section/page title row
    rule(label)                 horizontal divider
    footer(hint)                single-line bottom hint
    key_hints(*pairs)           keyboard shortcut strip
    panel(body, title, kind)    generic bordered panel
    error_panel(title, …)       cause / fix / detail error block
    success_panel(title, body)  green success block
    warning_panel(title, body)  amber warning block
    next_steps(title, …)        "what next" follow-up footer (Do→Suggest→Next)
    modal(body, title)          full-weight dialog block
    confirm_text(question, …)   yes/no prompt text (pair with prompt_toolkit)
    empty_state(msg, hint)      empty-list placeholder
    loading(msg)                spinner label string (for console.status)
    progress_bar(label, v, t)   inline labelled progress bar
    search_box(query)           search input display (pair with prompt_toolkit)

  Classes (stateful / composable):
    TableView    — data table with consistent column styling
    ListView     — selectable item list with focus indicator
    CommandPalette — command search result list

  Factory:
    make_console(**kwargs)      Console pre-configured with the Velune theme

Usage
─────
  from velune.cli.ui import header, error, success, TableView, ListView

  console.print(header("Providers", subtitle="3 active"))
  console.print(success("Model loaded"))
  console.print(error("Connection refused"))

  tv = TableView(["Model", "Status"])
  tv.add_row("claude-3.5-sonnet", "active")
  console.print(tv.render())
"""

from __future__ import annotations

from typing import Any

from rich import box
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from velune.cli import design

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _state_color(kind: str) -> str:
    """Map a semantic kind string to a hex color."""
    return {
        "info": design.INFO,
        "success": design.OK,
        "warning": design.WARN,
        "error": design.DANGER,
        "default": design.FAINT,
    }.get(kind, design.FAINT)


def _state_icon(kind: str) -> str:
    return {
        "info": design.ICON_INFO,
        "success": design.ICON_SUCCESS,
        "warning": design.ICON_WARNING,
        "error": design.ICON_ERROR,
    }.get(kind, design.ICON_INFO)


# ---------------------------------------------------------------------------
# Inline notifications — no border, one line
# ---------------------------------------------------------------------------


def notification(message: str, kind: str = "info") -> RenderableType:
    """Single-line inline notification with a semantic glyph prefix.

    Args:
        message: The notification body text.
        kind: "info" | "success" | "warning" | "error"

    Example::

        console.print(notification("Indexed 1 412 files", kind="success"))
        console.print(notification("Context 87 % full", kind="warning"))
    """
    color = _state_color(kind)
    glyph = _state_icon(kind)
    line = Text()
    line.append(f"  {glyph}  ", style=f"bold {color}")
    line.append(message, style=design.WHITE)
    return line


def success(message: str) -> RenderableType:
    """Green ✓ inline message."""
    return notification(message, kind="success")


def warning(message: str) -> RenderableType:
    """Amber ⚠ inline message."""
    return notification(message, kind="warning")


def error(message: str) -> RenderableType:
    """Red ✗ inline message."""
    return notification(message, kind="error")


def info(message: str) -> RenderableType:
    """Muted · inline message."""
    return notification(message, kind="info")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def header(
    title: str,
    subtitle: str | None = None,
    badge: str | None = None,
) -> RenderableType:
    """Section or page title row.

    Renders a bold title, an optional dim subtitle on the same line, and an
    optional bracketed badge (e.g. count) flush to the right of the subtitle.

    Example::

        console.print(header("Providers"))
        console.print(header("Providers", subtitle="3 active"))
        console.print(header("Model", badge="claude-3.5-sonnet"))
    """
    line = Text()
    line.append(f"  {title}", style=f"bold {design.WHITE}")
    if subtitle:
        line.append(f"  {subtitle}", style=design.MUTED)
    if badge:
        line.append(f"  [{badge}]", style=design.FAINT)
    return line


# ---------------------------------------------------------------------------
# Divider
# ---------------------------------------------------------------------------


def rule(label: str | None = None) -> RenderableType:
    """Horizontal divider with an optional centered label.

    Example::

        console.print(rule())
        console.print(rule("Configuration"))
    """
    if label:
        return Rule(
            f"  {label}  ",
            style=design.FAINT,
            characters="─",
        )
    return Rule(style=design.FAINT, characters="─")


# ---------------------------------------------------------------------------
# Footer / keyboard hints
# ---------------------------------------------------------------------------


def footer(hint: str) -> RenderableType:
    """Single-line bottom hint for non-interactive screens.

    Example::

        console.print(footer("Press ? for help  ·  q to quit"))
    """
    return Text(f"  {hint}", style=design.FAINT)


def key_hints(*pairs: tuple[str, str]) -> RenderableType:
    """Keyboard shortcut strip for interactive views.

    Each pair is (key, action).  The key is rendered in accent color; the
    action in muted; pairs are separated by the canonical · separator.

    Example::

        console.print(key_hints(
            ("tab", "select"),
            ("↑↓", "navigate"),
            ("enter", "confirm"),
            ("esc", "cancel"),
        ))
    """
    line = Text()
    line.append("  ")
    for i, (key, action) in enumerate(pairs):
        if i:
            line.append(design.SEP, style=design.FAINT)
        line.append(key, style=f"bold {design.ACCENT}")
        line.append(f" {action}", style=design.MUTED)
    return line


# ---------------------------------------------------------------------------
# Structural panels — bordered blocks
# ---------------------------------------------------------------------------


def panel(
    body: RenderableType,
    title: str | None = None,
    kind: str = "default",
    padding: tuple[int, int] = design.PADDING_DEFAULT,
) -> Panel:
    """Generic bordered panel.

    Args:
        body:    Renderable content.
        title:   Optional panel title (appears in the top border).
        kind:    "default" | "info" | "success" | "warning" | "error"
        padding: (vertical, horizontal) Rich padding tuple.

    Example::

        console.print(panel(Text("All checks passed"), title="Doctor", kind="success"))
    """
    color = _state_color(kind)
    title_markup = f"[bold {color}]{title}[/]" if title else None
    return Panel(
        body,
        title=title_markup,
        border_style=color,
        box=box.ROUNDED,
        padding=padding,
    )


def error_panel(
    title: str,
    cause: str | None = None,
    fix: list[str] | None = None,
    detail: str | None = None,
    docs_url: str | None = None,
) -> Panel:
    """Structured error panel with cause / fix / detail sections.

    Replaces ad-hoc Panel construction in rendering/error_panel.py.  All
    VeluneError rendering should route through here.

    Example::

        console.print(error_panel(
            "Connection refused",
            cause="Ollama not responding on port 11434",
            fix=["Start Ollama with: ollama serve", "Check firewall rules"],
        ))
    """
    body = Text()

    if cause:
        body.append("Cause\n", style=f"bold {design.WHITE}")
        body.append(f"  {cause}\n", style=f"dim {design.WHITE}")

    if fix:
        body.append("\nFix\n", style=f"bold {design.WHITE}")
        for step in fix:
            body.append(f"  {step}\n", style=f"dim {design.WHITE}")

    if docs_url:
        body.append(f"\n  docs {design.ICON_ARROW} {docs_url}", style=f"{design.INFO} underline")

    if detail:
        body.append("\n\n  Detail: ", style="dim")
        body.append(detail, style=f"dim {design.WHITE}")

    body.append("\n\n  Use --verbose for the full stack trace.", style=f"dim {design.MUTED}")

    return Panel(
        body,
        title=f"[bold {design.DANGER}]Error:[/] {title}",
        border_style=design.DANGER,
        box=box.ROUNDED,
        padding=design.PADDING_RELAXED,
    )


def success_panel(title: str, body: RenderableType) -> Panel:
    """Success panel with accent border.

    Example::

        console.print(success_panel("Setup complete", Text("Velune is ready.")))
    """
    return Panel(
        body,
        title=f"[bold {design.OK}]{title}[/]",
        border_style=design.OK,
        box=box.ROUNDED,
        padding=design.PADDING_DEFAULT,
    )


def warning_panel(title: str, body: RenderableType) -> Panel:
    """Warning panel with amber border.

    Example::

        console.print(warning_panel("Low disk space", Text("Free up space to continue.")))
    """
    return Panel(
        body,
        title=f"[bold {design.WARN}]{title}[/]",
        border_style=design.WARN,
        box=box.ROUNDED,
        padding=design.PADDING_DEFAULT,
    )


# ---------------------------------------------------------------------------
# Outcome footer — Do → Explain → Suggest → Next
# ---------------------------------------------------------------------------

# A single suggested follow-up: (label, command, optional one-line why).
Step = tuple[str, str, str | None]


def next_steps(
    title: str,
    summary: str,
    steps: list[Step],
    *,
    kind: str = "success",
) -> Panel:
    """Closing "what next" footer that turns an isolated command into a workflow.

    Generalizes the onboarding ``_stage_done`` pattern so any command can end with
    a summary plus an ordered list of concrete follow-up actions. Each step's
    command is rendered in accent so it reads as something the user can run next.

    Args:
        title:   Short outcome headline, e.g. "Provider added".
        summary: One-line recap of what just happened.
        steps:   Ordered ``(label, command, why)`` follow-ups; ``why`` may be None.
        kind:    Border/headline color — "success" (default) | "warning" | "info".

    Example::

        console.print(next_steps(
            "Provider added",
            "openai validated — 32 models available.",
            [
                ("Set as default model", "/model gpt-4o", None),
                ("Discover all models", "/models scan", "probe capabilities"),
            ],
        ))
    """
    color = _state_color(kind)
    body = Text()
    body.append(summary, style=design.MUTED)
    body.append("\n")
    for label, command, why in steps:
        body.append("\n")
        body.append(f"  {design.ICON_ARROW} ", style=color)
        body.append(label, style=design.WHITE)
        body.append("  ")
        body.append(command, style=f"bold {design.ACCENT}")
        if why:
            body.append(f"  {why}", style=design.FAINT)
    return Panel(
        body,
        title=f"[bold {color}]{title}[/]",
        border_style=color,
        box=box.ROUNDED,
        padding=design.PADDING_DEFAULT,
    )


# ---------------------------------------------------------------------------
# Modal dialog
# ---------------------------------------------------------------------------


def modal(
    body: RenderableType,
    title: str | None = None,
    subtitle: str | None = None,
) -> Panel:
    """Full-weight dialog panel — used for interactive views rendered inline.

    Uses relaxed padding and accent-colored title to signal interactivity.

    Example::

        console.print(modal(list_view.render(), title="Select Model"))
    """
    title_markup = f"[bold {design.ACCENT}]{title}[/]" if title else None
    sub_markup = f"[{design.FAINT}]{subtitle}[/]" if subtitle else None
    return Panel(
        body,
        title=title_markup,
        subtitle=sub_markup,
        border_style=design.FAINT,
        box=box.ROUNDED,
        padding=design.PADDING_RELAXED,
    )


# ---------------------------------------------------------------------------
# Confirmation dialog
# ---------------------------------------------------------------------------


def confirm_text(question: str, hint: str | None = None) -> RenderableType:
    """Confirmation prompt display text (pair with prompt_toolkit for actual input).

    Renders the question, an optional hint, and the [y] Yes / [n] No choices.

    Example::

        console.print(confirm_text(
            "Delete workspace?",
            hint="This cannot be undone.",
        ))
        answer = session.prompt("  › ")
    """
    body = Text()
    body.append(f"  {question}", style=f"bold {design.WHITE}")
    if hint:
        body.append(f"  {hint}", style=design.MUTED)
    body.append("\n\n")
    body.append("  [y] ", style=f"bold {design.OK}")
    body.append("Yes", style=design.WHITE)
    body.append("   ")
    body.append("[n] ", style=f"bold {design.DANGER}")
    body.append("No", style=design.WHITE)
    return body


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def empty_state(message: str, hint: str | None = None) -> RenderableType:
    """Placeholder shown when a list or table has no rows.

    Example::

        if not providers:
            console.print(empty_state(
                "No providers configured",
                hint="Run /provider add to get started.",
            ))
    """
    body = Text()
    body.append(f"\n  {message}\n", style=design.MUTED)
    if hint:
        body.append(f"\n  {hint}\n", style=design.FAINT)
    return body


# ---------------------------------------------------------------------------
# Loading state
# ---------------------------------------------------------------------------


def loading(message: str) -> str:
    """Spinner label string for use with ``console.status()``.

    Example::

        with console.status(loading("Connecting to Ollama…")):
            result = await connect()
    """
    return f"[{design.ACCENT}]{message}[/]"


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------


def progress_bar(
    label: str,
    value: float,
    total: float = 100.0,
    unit: str = "",
) -> RenderableType:
    """Inline labelled progress bar.

    Uses a 20-character Unicode bar and shows colour based on completion level
    (accent → warn → danger as value approaches total).

    Args:
        label:  Text label shown before the bar.
        value:  Current progress value.
        total:  Maximum value (default 100).
        unit:   Optional unit string appended after the count (e.g. "files").

    Example::

        console.print(progress_bar("Indexing", 45, 100, "files"))
        # →   Indexing  ████████░░░░░░░░░░░░  45%  45 / 100 files
    """
    pct = (value / total * 100) if total else 0.0
    filled = int(pct / 5)  # 20-char bar
    empty = 20 - filled

    color = (
        design.OK
        if pct < design.CTX_WARN_PCT
        else design.WARN
        if pct < design.CTX_DANGER_PCT
        else design.DANGER
    )

    line = Text()
    line.append(f"  {label}  ", style=design.MUTED)
    line.append("█" * filled, style=color)
    line.append("░" * empty, style=design.FAINT)
    line.append(f"  {pct:.0f}%", style=f"bold {color}")
    if total and unit:
        line.append(f"  {int(value)} / {int(total)} {unit}", style=design.MUTED)
    return line


# ---------------------------------------------------------------------------
# Search box (display-only — wire to prompt_toolkit for input)
# ---------------------------------------------------------------------------


def search_box(
    query: str = "",
    placeholder: str = "Type to search…",
) -> Panel:
    """Search input display.  Wire to prompt_toolkit for actual keystroke input.

    Renders an accent-bordered box with a > prompt.  When query is non-empty
    a block cursor is appended; when empty the placeholder is shown dim.

    Example::

        console.print(search_box(query=current_input))
    """
    body = Text()
    if query:
        body.append(f"  > {query}", style=design.WHITE)
        body.append(design.ICON_CURSOR, style=design.ACCENT)
    else:
        body.append("  > ", style=design.FAINT)
        body.append(placeholder, style=design.FAINT)
    return Panel(
        body,
        border_style=design.ACCENT,
        box=box.ROUNDED,
        padding=design.PADDING_COMPACT,
    )


# ---------------------------------------------------------------------------
# TableView
# ---------------------------------------------------------------------------


class TableView:
    """Data table with consistent Velune styling.

    Columns are left-aligned by default; the first column is rendered in white,
    subsequent columns in muted.  No inner vertical lines (SIMPLE_HEAD box).

    Example::

        tv = TableView(["Model", "Status", "Provider"])
        tv.add_row("claude-3.5-sonnet", "active", "Anthropic")
        tv.add_row("gpt-4o", "active", "OpenAI")
        console.print(tv.render())
    """

    def __init__(
        self,
        columns: list[str],
        title: str | None = None,
        show_header: bool = True,
        expand: bool = False,
    ) -> None:
        self._columns = columns
        self._title = title
        self._show_header = show_header
        self._expand = expand
        self._rows: list[tuple[Any, ...]] = []
        self._row_styles: list[str | None] = []

    def add_row(self, *values: Any, style: str | None = None) -> None:
        """Append a data row.  Pass ``style`` to override per-row colour."""
        self._rows.append(tuple(str(v) for v in values))
        self._row_styles.append(style)

    def render(self) -> Table:
        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=self._show_header,
            header_style=f"bold {design.WHITE}",
            border_style=design.FAINT,
            title=f"[bold {design.ACCENT}]{self._title}[/]" if self._title else None,
            title_style=f"bold {design.ACCENT}",
            padding=design.PADDING_COMPACT,
            expand=self._expand,
            show_edge=False,
        )
        for i, col in enumerate(self._columns):
            col_style = design.WHITE if i == 0 else design.MUTED
            table.add_column(col, style=col_style, no_wrap=False)
        for row, style in zip(self._rows, self._row_styles, strict=False):
            table.add_row(*row, style=style or "")
        return table


# ---------------------------------------------------------------------------
# ListView
# ---------------------------------------------------------------------------


class ListView:
    """Selectable item list with a ▶ focus indicator.

    Set ``selected`` to the 0-based index of the currently highlighted item
    (-1 = no selection).

    Example::

        lv = ListView(["claude-3.5-sonnet", "gpt-4o", "llama3.2"])
        lv.selected = 0
        console.print(lv.render())
    """

    def __init__(
        self,
        items: list[str],
        selected: int = -1,
        show_index: bool = False,
    ) -> None:
        self.items = items
        self.selected = selected
        self.show_index = show_index

    def render(self) -> Text:
        text = Text()
        for i, item in enumerate(self.items):
            is_selected = i == self.selected
            if is_selected:
                text.append(f"  {design.ICON_SELECTED} ", style=f"bold {design.ACCENT}")
                text.append(item, style=f"bold {design.WHITE}")
            else:
                text.append("    ")
                if self.show_index:
                    text.append(f"{i + 1}  ", style=design.FAINT)
                text.append(item, style=design.MUTED)
            text.append("\n")
        return text


# ---------------------------------------------------------------------------
# CommandPalette
# ---------------------------------------------------------------------------


class CommandPalette:
    """Command search result list rendered as a bordered panel.

    Example::

        cp = CommandPalette()
        cp.add("/help",     "Show available commands")
        cp.add("/model",    "Switch AI model")
        cp.add("/provider", "Manage providers")
        console.print(cp.render())
    """

    def __init__(self, title: str = "Commands", query: str = "") -> None:
        self._title = title
        self._query = query
        self._entries: list[tuple[str, str]] = []

    def add(self, command: str, description: str) -> None:
        self._entries.append((command, description))

    def render(self) -> Panel:
        body = Text()

        # Inline search prompt
        if self._query:
            body.append(f"  > {self._query}\n\n", style=design.WHITE)
        else:
            body.append("  > \n\n", style=design.FAINT)

        if not self._entries:
            body.append(
                f"  {design.ICON_INFO}  No commands match.\n",
                style=design.MUTED,
            )
        else:
            cmd_width = max(len(cmd) for cmd, _ in self._entries) + 2
            for cmd, desc in self._entries:
                body.append(f"  {cmd:<{cmd_width}}", style=f"bold {design.ACCENT}")
                body.append(f"  {desc}\n", style=design.MUTED)

        return Panel(
            body,
            title=f"[{design.FAINT}]{self._title}[/]",
            border_style=design.FAINT,
            box=box.ROUNDED,
            padding=design.PADDING_COMPACT,
        )


# ---------------------------------------------------------------------------
# Console factory
# ---------------------------------------------------------------------------


def make_console(**kwargs: Any) -> Console:
    """Return a ``Console`` pre-configured with the Velune Rich theme.

    Pass any ``rich.console.Console`` keyword arguments to override defaults.

    Example::

        console = make_console(highlight=False)
    """
    from velune.cli.display.themes import VeluneTheme

    return Console(theme=VeluneTheme.get_theme(), **kwargs)
