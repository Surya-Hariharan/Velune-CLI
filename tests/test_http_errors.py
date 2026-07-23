"""Retry-After header parsing, and adapters mapping HTTP 429 -> RateLimitError."""

from __future__ import annotations

import httpx
import pytest

from velune.core.errors.provider import RateLimitError
from velune.providers.adapters._http_errors import parse_retry_after
from velune.providers.adapters.anthropic import AnthropicProvider
from velune.providers.adapters.openai import OpenAIProvider


def _headers(retry_after: str | None) -> httpx.Headers:
    return httpx.Headers({"retry-after": retry_after} if retry_after else {})


def test_parse_retry_after_numeric_seconds():
    assert parse_retry_after(_headers("30")) == 30.0


def test_parse_retry_after_http_date(monkeypatch):
    import datetime

    fixed_now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    class _FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("velune.providers.adapters._http_errors.datetime", _FixedDatetime)
    future = "Thu, 01 Jan 2026 00:00:30 GMT"
    assert parse_retry_after(_headers(future)) == pytest.approx(30.0, abs=1.0)


def test_parse_retry_after_missing_or_garbage_is_none():
    assert parse_retry_after(_headers(None)) is None
    assert parse_retry_after(_headers("not-a-value")) is None


def _status_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_openai_raises_rate_limit_error_with_retry_after():
    provider = OpenAIProvider(api_key="sk-test")
    exc = _status_error(429, {"retry-after": "5"})
    with pytest.raises(RateLimitError) as exc_info:
        provider._raise_provider_error(exc, "completion")
    assert exc_info.value.retry_after == 5.0


def test_openai_rate_limit_without_header_has_none_retry_after():
    provider = OpenAIProvider(api_key="sk-test")
    exc = _status_error(429)
    with pytest.raises(RateLimitError) as exc_info:
        provider._raise_provider_error(exc, "completion")
    assert exc_info.value.retry_after is None


def test_anthropic_raises_rate_limit_error_with_retry_after():
    provider = AnthropicProvider(api_key="sk-ant-test")
    exc = _status_error(429, {"retry-after": "12"})
    with pytest.raises(RateLimitError) as exc_info:
        provider._raise_provider_error(exc, "completion")
    assert exc_info.value.retry_after == 12.0


def test_groq_inherits_rate_limit_handling_from_openai():
    from velune.providers.adapters.groq import GroqProvider

    provider = GroqProvider(api_key="gsk-test")
    exc = _status_error(429, {"retry-after": "7"})
    with pytest.raises(RateLimitError) as exc_info:
        provider._raise_provider_error(exc, "completion")
    assert exc_info.value.retry_after == 7.0
