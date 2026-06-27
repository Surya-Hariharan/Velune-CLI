"""Rich error panel renderer for structured VeluneError display.

All panel construction delegates to ``velune.cli.ui`` so errors share the same
borders, colours, and spacing as every other screen in the application.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel

from velune.cli import ui

if TYPE_CHECKING:
    from velune.core.errors.catalog import VeluneError

_BUG_REPORT_URL = "https://github.com/velune-ai/velune/issues"


def render_error(error: VeluneError) -> Panel:
    """Build a Rich Panel for a VeluneError with cause and fix sections."""
    return ui.error_panel(
        title=error.title,
        cause=error.get_cause() or None,
        fix=list(error.fix) if error.fix else None,
        detail=(
            error.get_detail()
            if error.get_detail() and error.get_detail() != error.title
            else None
        ),
        docs_url=error.docs_url or None,
    )


def render_unexpected_error(exc: Exception) -> Panel:
    """Build a Rich Panel for an unrecognised exception."""
    return ui.error_panel(
        title="Unexpected Error",
        cause=f"{type(exc).__name__}: {exc}",
        fix=[
            "Use --verbose to see the full stack trace",
            f"Report the issue at {_BUG_REPORT_URL}",
            "Include the --verbose output in your report",
        ],
    )


def print_error(error: VeluneError, console: Console | None = None) -> None:
    """Convenience wrapper: render and print a VeluneError to the given console."""
    (console or Console()).print(render_error(error))


def print_unexpected_error(exc: Exception, console: Console | None = None) -> None:
    """Convenience wrapper: render and print an unexpected exception."""
    (console or Console()).print(render_unexpected_error(exc))
