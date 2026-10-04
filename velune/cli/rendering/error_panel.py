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
            error.get_detail() if error.get_detail() and error.get_detail() != error.title else None
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


_PROVIDER_ERROR_TITLES: dict[str, str] = {
    "ModelNotFoundError": "Model or Endpoint Not Found",
    "InvalidRequestError": "Request Rejected",
    "ProviderAuthenticationError": "Authentication Failed",
    "RateLimitError": "Rate Limited",
    "ProviderConnectionError": "Connection Failed",
    "ProviderTimeoutError": "Provider Timed Out",
    "InferenceError": "Provider Request Failed",
}


_DOCTOR_HINT = "Run /doctor for provider diagnostics"

# The first line is the actual remedy for each failure; /doctor is the fallback.
_PROVIDER_ERROR_FIXES: dict[str, list[str]] = {
    "ProviderAuthenticationError": [
        "Run /connect to enter a new API key for this provider",
        _DOCTOR_HINT,
    ],
    "ModelNotFoundError": [
        "Pick another model with /model",
        "The provider may have retired it; /doctor shows what is available",
    ],
    "RateLimitError": ["Wait a moment and retry, or switch model with /model"],
    "ProviderConnectionError": ["Check your network connection and the provider's status page"],
    "ProviderTimeoutError": ["Retry, or switch to a faster model with /model", _DOCTOR_HINT],
}


def render_provider_error(exc: Exception) -> Panel:
    """Build a Rich Panel for a provider/inference failure.

    A provider being down, rejecting a decommissioned model, or hitting a
    rate limit is an expected, everyday failure mode — not a Velune bug. It
    must not be rendered as :func:`render_unexpected_error`'s "Unexpected
    Error... report an issue" panel, which misattributes the provider's
    behavior to Velune and sends the user down the wrong path. This gives one
    clear, actionable line instead, with ``/doctor`` as the next step for
    anyone who needs more than that.
    """
    kind = type(exc).__name__
    title = _PROVIDER_ERROR_TITLES.get(kind, "Provider Request Failed")
    return ui.error_panel(
        title=title,
        cause=str(exc),
        fix=_PROVIDER_ERROR_FIXES.get(kind, [_DOCTOR_HINT]),
    )


def print_error(error: VeluneError, console: Console | None = None) -> None:
    """Convenience wrapper: render and print a VeluneError to the given console."""
    (console or Console()).print(render_error(error))


def print_unexpected_error(exc: Exception, console: Console | None = None) -> None:
    """Convenience wrapper: render and print an unexpected exception."""
    (console or Console()).print(render_unexpected_error(exc))
