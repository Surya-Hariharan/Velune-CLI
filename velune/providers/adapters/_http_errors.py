"""Shared Retry-After parsing for adapters that want real 429 awareness.

Kept separate from ``_toolcalls.py`` (tool-call wire helpers) since this is
about HTTP error translation, used by adapters that raise
:class:`velune.core.errors.provider.RateLimitError` on a 429 response instead
of the generic :class:`~velune.core.errors.provider.InferenceError`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import NoReturn

import httpx


def parse_retry_after(headers: httpx.Headers) -> float | None:
    """Parse a ``Retry-After`` response header into seconds.

    Accepts either form the spec allows — an integer number of seconds, or an
    HTTP-date to wait until. Returns ``None`` when the header is absent or
    unparseable, so callers fall back to their own exponential backoff rather
    than failing because of this alone.
    """
    raw = headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def raise_typed_http_error(provider_label: str, exc: httpx.HTTPError, action: str) -> NoReturn:
    """Translate an httpx failure into the right typed provider error, then raise it.

    Each branch is a *deterministic* verdict (about the key, the rate limit, or
    the request) and is excluded from ``RETRYABLE_EXCEPTIONS`` — retrying a
    decommissioned model id or a malformed request just delays the same
    failure. Only the final generic :class:`InferenceError` (5xx, network
    hiccups, anything unclassified) is retryable. Always raises.
    """
    from velune.core.errors.provider import (
        InferenceError,
        InvalidRequestError,
        ModelNotFoundError,
        ProviderAuthenticationError,
        RateLimitError,
    )

    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    if status in (401, 403):
        raise ProviderAuthenticationError(
            f"{provider_label} rejected the API key (HTTP {status}) during {action}."
        ) from exc
    if status == 429:
        assert isinstance(exc, httpx.HTTPStatusError)
        raise RateLimitError(
            f"{provider_label} rate-limited (HTTP 429) during {action}.",
            retry_after=parse_retry_after(exc.response.headers),
        ) from exc
    if status == 404:
        raise ModelNotFoundError(
            f"{provider_label} returned HTTP 404 during {action} — the model or "
            f"endpoint was not found. It may be mistyped, decommissioned, or "
            f"renamed by the provider; this is not retried automatically."
        ) from exc
    if status in (400, 422):
        raise InvalidRequestError(
            f"{provider_label} rejected the request as malformed (HTTP {status}) during {action}."
        ) from exc
    raise InferenceError(f"{provider_label} {action} failed: {exc}") from exc
