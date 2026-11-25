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
    panel(body, title, kind)    generic bordered panel
    error_panel(title, …)       cause / fix / detail error block
    next_steps(title, …)        "what next" follow-up footer (Do→Suggest→Next)
    loading(msg)                spinner label string (for console.status)

  Note: the interactive command palette lives in ``velune.cli.command_palette``
  (prompt_toolkit-based, with fuzzy search and favorites) — not here. Static
  table/list rendering needs are met inline with ``rich.table.Table`` directly.

Usage
─────
  from velune.cli.ui import header, error, success

  console.print(header("Providers", subtitle="3 active"))
  console.print(success("Model loaded"))
  console.print(error("Connection refused"))

  tv = TableView(["Model", "Status"])
  tv.add_row("claude-3.5-sonnet", "active")
  console.print(tv.render())
"""

from __future__ import annotations

from rich import box
from rich.console import RenderableType
from rich.panel import Panel
from rich.rule import Rule
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


# ---------------------------------------------------------------------------
# Confirmation dialog
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Search box (display-only — wire to prompt_toolkit for input)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Console factory
# ---------------------------------------------------------------------------
