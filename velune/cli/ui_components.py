"""Centralized, minimalist UI components for Velune CLI.

This module enforces consistent spacing, typography, borders, loading states,
and notifications across all interactive and non-interactive screens.
"""

from __future__ import annotations

import typing
from contextlib import contextmanager

from rich.table import Table

if typing.TYPE_CHECKING:
    from rich.console import Console


def create_table(*columns: str, title: str | None = None) -> Table:
    """Create a minimalist, borderless table.

    Args:
        *columns: Column header titles.
        title: Optional title for the table.

    Returns:
        A pre-configured rich Table object with no borders and standard padding.
    """
    table = Table(
        title=title,
        show_header=bool(columns),
        box=None,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        title_style="bold cyan",
        title_justify="left",
    )
    for col in columns:
        table.add_column(col, style="dim")
    return table


def print_notification(console: Console, message: str, type: str = "info") -> None:
    """Print a standardized single-line notification.

    Args:
        console: The rich Console to print to.
        message: The message body.
        type: One of 'success', 'warning', 'error', 'info'.
    """
    markers = {
        "success": "[green]✓[/green]",
        "warning": "[yellow]⚠[/yellow]",
        "error": "[red]✗[/red]",
        "info": "[cyan]ℹ[/cyan]",
    }
    marker = markers.get(type, markers["info"])
    console.print(f"  {marker}  {message}")


def print_header(console: Console, title: str, subtitle: str | None = None) -> None:
    """Print a standardized screen header with minimal spacing.

    Args:
        console: The rich Console to print to.
        title: Main screen title.
        subtitle: Optional secondary descriptor.
    """
    console.print()
    console.print(f"[bold white]{title}[/bold white]")
    if subtitle:
        console.print(f"[dim]{subtitle}[/dim]")
    console.print(f"[dim]{'—' * 60}[/dim]")


@contextmanager
def loading_status(console: Console, message: str) -> typing.Iterator[None]:
    """A minimalist loading spinner context manager.

    Args:
        console: The rich Console.
        message: Status message.
    """
    # Use dots spinner with dim styling for low cognitive load
    with console.status(f"[dim]{message}[/dim]", spinner="dots"):
        yield
