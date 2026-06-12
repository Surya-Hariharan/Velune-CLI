"""Rich error panel renderer for structured VeluneError display."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from velune.core.errors.catalog import VeluneError

_BUG_REPORT_URL = "https://github.com/velune-ai/velune/issues"


def render_error(error: VeluneError) -> Panel:
    """Build a Rich Panel for a VeluneError with cause and fix sections."""
    body = Text()

    cause = error.get_cause()
    if cause:
        body.append("Cause\n", style="bold dim white")
        body.append(f"  {cause}\n", style="dim white")

    if error.fix:
        body.append("\nFix\n", style="bold yellow")
        for step in error.fix:
            body.append(f"  • {step}\n", style="yellow")

    if error.docs_url:
        body.append(f"\n  docs → {error.docs_url}", style="dim cyan underline")

    detail = error.get_detail()
    if detail and detail != error.title:
        body.append("\n\n  Detail: ", style="dim")
        body.append(detail, style="dim white")

    body.append("\n\n  Run with --verbose to see the full stack trace.", style="dim")

    return Panel(
        body,
        title=f"[bold red]✗  {error.title}[/bold red]",
        border_style="red",
        padding=(1, 2),
    )


def render_unexpected_error(exc: Exception) -> Panel:
    """Build a Rich Panel for an unrecognised exception."""
    body = Text()
    body.append(
        "Velune encountered an error it did not anticipate.\n\n",
        style="dim white",
    )
    body.append(f"  {type(exc).__name__}: ", style="dim white")
    body.append(f"{exc}\n", style="white")

    body.append("\nFix\n", style="bold yellow")
    body.append("  • Run with --verbose to see the full stack trace\n", style="yellow")
    body.append(f"  • File a bug report at {_BUG_REPORT_URL}\n", style="yellow")
    body.append("  • Include the --verbose output when reporting\n", style="yellow")

    return Panel(
        body,
        title="[bold red]✗  Unexpected error[/bold red]",
        border_style="red",
        padding=(1, 2),
    )


def print_error(error: VeluneError, console: Console | None = None) -> None:
    """Convenience wrapper: render and print a VeluneError to the given console."""
    (console or Console()).print(render_error(error))


def print_unexpected_error(exc: Exception, console: Console | None = None) -> None:
    """Convenience wrapper: render and print an unexpected exception."""
    (console or Console()).print(render_unexpected_error(exc))
