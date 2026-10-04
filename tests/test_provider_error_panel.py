"""Each provider failure shows the fix that actually resolves it, not a generic /doctor pointer."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from velune.cli.rendering.error_panel import render_provider_error
from velune.core.errors.provider import (
    InferenceError,
    ModelNotFoundError,
    ProviderAuthenticationError,
    ProviderTimeoutError,
    RateLimitError,
)


def _text(exc: Exception) -> str:
    buf = io.StringIO()
    Console(file=buf, width=100, force_terminal=False).print(render_provider_error(exc))
    return buf.getvalue()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProviderAuthenticationError("groq rejected the API key"), "/connect"),
        (ModelNotFoundError("model gone"), "/model"),
        (RateLimitError("slow down"), "retry"),
        (ProviderTimeoutError("too slow"), "/model"),
    ],
)
def test_fix_matches_the_failure(exc, expected):
    assert expected in _text(exc)


def test_unclassified_errors_still_point_to_doctor():
    assert "/doctor" in _text(InferenceError("boom"))
