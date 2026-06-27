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
        body.append("Cause\n", style="bold white")
        body.append(f"  {cause}\n", style="dim white")

    if error.fix:
        body.append("\nFix\n", style="bold white")
        for step in error.fix:
            body.append(f"  {step}\n", style="dim white")

    if error.docs_url:
        body.append(f"\n  docs → {error.docs_url}", style="blue underline")

    detail = error.get_detail()
    if detail and detail != error.title:
        body.append("\n\n  Detail: ", style="dim")
        body.append(detail, style="dim white")

    body.append("\n\n  Use --verbose for full stack trace.", style="dim")

    return Panel(
        body,
        title=f"[bold red]Error:[/bold red] {error.title}",
        border_style="red",
        padding=(1, 2),
    )


def render_unexpected_error(exc: Exception) -> Panel:
    """Build a Rich Panel for an unrecognised exception."""
    body = Text()
    body.append(
        "An unexpected error occurred.\n\n",
        style="dim white",
    )
    body.append(f"  {type(exc).__name__}: ", style="dim white")
    body.append(f"{exc}\n", style="white")

    body.append("\nWhat to do\n", style="bold white")
    body.append("  Use --verbose to see the full stack trace\n", style="dim white")
    body.append(f"  Report the issue at {_BUG_REPORT_URL}\n", style="dim white")
    body.append("  Include the --verbose output in your report\n", style="dim white")

    return Panel(
        body,
        title="[bold red]Unexpected Error[/bold red]",
        border_style="red",
        padding=(1, 2),
    )


def print_error(error: VeluneError, console: Console | None = None) -> None:
    """Convenience wrapper: render and print a VeluneError to the given console."""
    (console or Console()).print(render_error(error))


def print_unexpected_error(exc: Exception, console: Console | None = None) -> None:
    """Convenience wrapper: render and print an unexpected exception."""
    (console or Console()).print(render_unexpected_error(exc))
