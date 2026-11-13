"""Turning a raw response into the structured, redacted result the model sees.

The model never receives an httpx object or raw bytes. It receives plain JSON:
status, filtered headers, content type, a bounded body (parsed JSON when it is
JSON), timing, redirect information and the request identifier. Everything that
could carry a credential is masked here:

* header values for ``Authorization``, ``Set-Cookie``, ``X-API-Key`` and friends;
* query-string values of sensitive parameters (``api_key``, ``token``, …) in every
  URL that is echoed back, including redirect locations;
* JSON fields with sensitive names (``password``, ``access_token``, …) plus any
  names the user listed in ``redact_response_fields``;
* every string passes through the runtime secret registry, so a credential that
  was loaded this session is masked even where it has no recognizable shape.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from velune.core.redaction import REDACTION_PLACEHOLDER, is_sensitive_key, redact_secrets
from velune.resources.http.assertions import ResponseView
from velune.resources.http.client import HttpOutcome

MAX_BODY_CHARS = 6000
MAX_HEADER_VALUE = 300
MAX_HEADERS = 40
_SENSITIVE_QUERY = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "auth",
        "authorization",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "sig",
        "signature",
        "code",
        "session",
        "sessionid",
        "x-api-key",
    }
)
_TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "+json",
    "+xml",
    "application/x-www-form-urlencoded",
    "application/graphql",
)


def redact_url(url: str) -> str:
    """Mask the values of sensitive query parameters (and URL passwords) in *url*."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_secrets(url)
    if not parts.query:
        return redact_secrets(url)
    pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        sensitive = key.lower() in _SENSITIVE_QUERY or is_sensitive_key(key)
        pairs.append((key, REDACTION_PLACEHOLDER if sensitive and value else value))
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='*')}" for k, v in pairs)
    return redact_secrets(urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")))


def charset_of(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.lower() == "charset" and value:
            return value.strip("\"' ").lower()
    return "utf-8"


def is_textual(content_type: str) -> bool:
    lowered = content_type.lower()
    return not lowered or any(marker in lowered for marker in _TEXT_TYPES)


def decode_body(body: bytes, content_type: str) -> str | None:
    """The body as text, or None when it is binary."""
    if not body:
        return ""
    if not is_textual(content_type):
        return None
    try:
        return body.decode(charset_of(content_type))
    except (LookupError, UnicodeDecodeError):
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return None


def _mask_fields(value: Any, names: frozenset[str], depth: int = 0) -> Any:
    if depth > 32:
        return REDACTION_PLACEHOLDER
    if isinstance(value, dict):
        return {
            k: REDACTION_PLACEHOLDER
            if isinstance(k, str) and k.lower() in names and v not in (None, "", [], {})
            else _mask_fields(v, names, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_fields(v, names, depth + 1) for v in value]
    return value


def clean_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    """Response headers as ``name -> value`` (repeats joined), secrets masked."""
    merged: dict[str, str] = {}
    for name, value in headers[:100]:
        key = name.lower()
        merged[key] = f"{merged[key]}, {value}" if key in merged else value
    shown: dict[str, str] = {}
    for key, value in list(merged.items())[:MAX_HEADERS]:
        if is_sensitive_key(key):
            shown[key] = REDACTION_PLACEHOLDER
        elif key == "location":
            shown[key] = redact_url(value)[:MAX_HEADER_VALUE]
        else:
            shown[key] = redact_secrets(value)[:MAX_HEADER_VALUE]
    return shown


def status_outcome(status: int) -> str:
    """The category a status code falls in."""
    if status == 401:
        return "authentication_failed"
    if status == 403:
        return "authorization_denied"
    if 200 <= status < 300:
        return "success"
    if 300 <= status < 400:
        return "redirect"
    if 400 <= status < 500:
        return "client_error"
    if status >= 500:
        return "server_error"
    return "informational"


def view_of(outcome: HttpOutcome) -> tuple[ResponseView, str | None]:
    """The assertion view of a response, and its text (None for binary)."""
    content_type = outcome.header("content-type") or ""
    text = decode_body(outcome.body, content_type)
    headers = {k.lower(): v for k, v in outcome.headers}
    return (
        ResponseView(outcome.status, headers, text or "", outcome.duration_ms),
        text,
    )


def parse_json_body(text: str | None, content_type: str) -> tuple[bool, Any]:
    """``(is_json, value)``: parse when the type says JSON or the text looks like it."""
    if text is None or not text.strip():
        return False, None
    lowered = content_type.lower()
    looks = text.lstrip()[:1] in ("{", "[")
    if "json" not in lowered and not looks:
        return False, None
    try:
        return True, json.loads(text)
    except ValueError:
        return False, None


def present_body(
    outcome: HttpOutcome, redact_fields: frozenset[str]
) -> tuple[dict[str, Any], Any, str | None]:
    """The model-facing body fields, the parsed JSON (unredacted, for local use), and text."""
    content_type = outcome.header("content-type") or ""
    text = decode_body(outcome.body, content_type)
    fields: dict[str, Any] = {
        "content_type": content_type.split(";")[0].strip() or None,
        "size_bytes": len(outcome.body),
    }
    if text is None:
        fields["body"] = None
        fields["binary"] = True
        return fields, None, None
    is_json, document = parse_json_body(text, content_type)
    if is_json:
        shown = _mask_fields(document, redact_fields) if redact_fields else document
        from velune.core.redaction import redact_structure

        shown = redact_structure(shown)
        rendered = json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= MAX_BODY_CHARS:
            fields["json"] = shown
        else:
            fields["json_preview"] = rendered[:MAX_BODY_CHARS]
            fields["body_truncated"] = True
    else:
        shown_text = redact_secrets(text)
        fields["text"] = shown_text[:MAX_BODY_CHARS]
        if len(shown_text) > MAX_BODY_CHARS:
            fields["body_truncated"] = True
    if content_type and "json" in content_type.lower() and not is_json and text.strip():
        fields["json_error"] = "the body is labelled JSON but is not valid JSON"
    return fields, document if is_json else None, text


def select_paths(document: Any, paths: list[str]) -> dict[str, Any]:
    """Values at the requested JSON paths (secrets masked; missing paths say so)."""
    from velune.core.redaction import redact_structure
    from velune.resources.http.assertions import is_missing, json_lookup

    selected: dict[str, Any] = {}
    for path in paths[:20]:
        value = json_lookup(document, path)
        selected[path] = "<missing>" if is_missing(value) else redact_structure(value)
    return selected


def redirect_entries(redirects: list[dict[str, object]]) -> list[dict[str, object]]:
    """Redirect hops with every URL's secrets masked."""
    cleaned = []
    for hop in redirects:
        entry = dict(hop)
        for key in ("from", "to"):
            if isinstance(entry.get(key), str):
                entry[key] = redact_url(entry[key])  # type: ignore[arg-type]
        cleaned.append(entry)
    return cleaned


# ── Keeping URLs out of library logs ────────────────────────────────────────

_URL_IN_TEXT = re.compile(r"""https?://[^\s"']+""")


class UrlLogRedactor(logging.Filter):
    """Mask sensitive query values in any URL a library logs.

    httpx logs ``HTTP Request: GET <url>`` at INFO, query string included — which
    is where an API key or a model-typed ``?token=`` would be. The CLI sets that
    logger to WARNING, but the integration must not depend on every entry point
    doing so.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "://" in message:
            redacted = _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), message)
            if redacted != message:
                record.msg, record.args = redacted, None
        return True


def install_log_redaction() -> None:
    """Attach :class:`UrlLogRedactor` to the HTTP libraries' loggers (idempotent)."""
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, UrlLogRedactor) for f in target.filters):
            target.addFilter(UrlLogRedactor())
