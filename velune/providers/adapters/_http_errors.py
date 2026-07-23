"""Shared Retry-After parsing for adapters that want real 429 awareness.

Kept separate from ``_toolcalls.py`` (tool-call wire helpers) since this is
about HTTP error translation, used by adapters that raise
:class:`velune.core.errors.provider.RateLimitError` on a 429 response instead
of the generic :class:`~velune.core.errors.provider.InferenceError`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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
