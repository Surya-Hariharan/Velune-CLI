"""Secret redaction for logs and any text that may surface provider credentials.

Velune is BYOK: API keys live in the OS keyring or environment and flow through
provider adapters. A stray ``logger.debug("request headers: %s", headers)`` or an
HTTP-client exception that echoes an ``Authorization`` header would otherwise
leak those keys into log files, JSON log shippers, or the terminal scrollback.

This module provides:

* :func:`redact_secrets` — scrub a string of known credential shapes and the
  live values of any configured provider env vars.
* :class:`SecretRedactingFilter` — a ``logging.Filter`` that runs every emitted
  record's final message through :func:`redact_secrets`. Installed on the root
  handlers in :func:`velune.core.logging.configure_logging`.

The patterns are deliberately high-precision (long, prefix-anchored tokens) so
normal log text is never garbled.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

REDACTION_PLACEHOLDER = "***REDACTED***"

# ── Runtime secret registry ──────────────────────────────────────────────────
# Integration credentials (a database password, an API token, a Supabase key)
# have no recognizable shape, so pattern matching alone cannot catch them. Any
# code that *resolves* such a value registers it here; from then on every
# redaction pass — tool results, logs, approval previews — masks it exactly.
_MIN_REGISTERED_LENGTH = 6
_registry_lock = threading.Lock()
_registered: set[str] = set()


def register_secret_value(value: Any) -> None:
    """Mask *value* wherever it appears in redacted text from now on.

    Values shorter than six characters are ignored: masking them would garble
    ordinary text and they cannot be meaningfully protected by redaction.
    """
    if not isinstance(value, str):
        return
    value = value.strip()
    if len(value) < _MIN_REGISTERED_LENGTH:
        return
    with _registry_lock:
        _registered.add(value)


def unregister_secret_value(value: Any) -> None:
    if isinstance(value, str):
        with _registry_lock:
            _registered.discard(value.strip())


def _registered_snapshot() -> list[str]:
    with _registry_lock:
        # Longest first, so a secret that contains another is masked whole.
        return sorted(_registered, key=len, reverse=True)


# Provider key shapes. Each is prefix-anchored and requires a long opaque tail
# so ordinary words ("skipped", "token budget") are never matched.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),  # OpenAI project keys
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI / generic sk-
    re.compile(r"xai-[A-Za-z0-9]{20,}"),  # xAI
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"hf_[A-Za-z0-9]{20,}"),  # Hugging Face
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),  # Google API keys
    re.compile(r"r8_[A-Za-z0-9]{20,}"),  # Replicate
    re.compile(r"fw_[A-Za-z0-9]{20,}"),  # Fireworks
    re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"),  # OpenRouter
    # Authorization headers: "Bearer <token>" / "Authorization: Token <token>"
    re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}"),
)

#: Env vars whose live values must be scrubbed even if they don't match a shape
#: above (e.g. a custom self-hosted gateway key). Mirrors keystore env vars.
_SECRET_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "HF_TOKEN",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
)


def redact_secrets(text: str) -> str:
    """Return *text* with any recognizable secret replaced by a placeholder.

    Scrubs both well-known key shapes and the literal current values of
    configured provider environment variables.
    """
    if not text:
        return text

    # Redact live env-var values first — these are exact, highest-confidence.
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value and len(value) >= 8 and value in text:
            text = text.replace(value, REDACTION_PLACEHOLDER)

    # Then values registered at runtime (resolved integration credentials).
    for value in _registered_snapshot():
        if value in text:
            text = text.replace(value, REDACTION_PLACEHOLDER)

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTION_PLACEHOLDER, text)

    # Keep the key / URL structure readable; mask only the secret part.
    text = _URL_CREDENTIALS.sub(rf"\1{REDACTION_PLACEHOLDER}@", text)
    text = _KEY_VALUE_SECRET.sub(rf"\1{REDACTION_PLACEHOLDER}", text)

    return text


# ``scheme://user:password@host`` — the password segment.
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+@")
# ``password=…`` / ``"api_key": "…"`` style pairs in connection strings, config
# dumps and JSON. The value must be at least 4 non-space characters.
_KEY_VALUE_SECRET = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|service[_-]?role[_-]?key)[\"']?\s*[=:]\s*[\"']?)"
    r"(?!\*\*\*REDACTED)[^\s\"',;&}]{4,}"
)

# Keys whose *values* are secret regardless of what the value looks like.
_SENSITIVE_KEY = re.compile(
    r"(?i)^(?:x-)?(?:password|passwd|pwd|secret|token|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|auth[_-]?token|api[_-]?key|apikey|x-api-key|authorization|"
    r"proxy-authorization|cookie|set-cookie|session[_-]?id|client[_-]?secret|"
    r"private[_-]?key|service[_-]?role[_-]?key|service[_-]?key|anon[_-]?key|"
    r"access[_-]?key|secret[_-]?key|credentials?|dsn|connection[_-]?string)$"
)
_MAX_REDACT_DEPTH = 32


def is_sensitive_key(key: Any) -> bool:
    """True when a mapping key names a field whose value must never be shown."""
    return isinstance(key, str) and bool(_SENSITIVE_KEY.match(key.strip()))


def redact_structure(value: Any, _depth: int = 0) -> Any:
    """Return a copy of *value* (dict/list/str/scalars) with secrets masked.

    Values under sensitive keys (password, token, authorization, cookie, …) are
    replaced outright; every string is also passed through :func:`redact_secrets`.
    Structures nested deeper than a fixed limit are replaced by a placeholder
    rather than walked, so a hostile payload cannot exhaust the stack.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return REDACTION_PLACEHOLDER
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key) and item not in (None, "", [], {}):
                out[key] = REDACTION_PLACEHOLDER
            else:
                out[key] = redact_structure(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [redact_structure(item, _depth + 1) for item in value]
        return items if isinstance(value, list) else tuple(items)
    return value


_TRACEBACK_FORMATTER = logging.Formatter()


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from every record's rendered message.

    Rewrites ``record.msg`` to the fully-formatted, redacted string and clears
    ``record.args`` so downstream formatters don't re-expand the original
    (unredacted) arguments.

    A traceback is rendered by the formatter from ``record.exc_info`` *after*
    filters run, so an exception whose text carries a secret would otherwise
    bypass the message redaction. When the rendered traceback contains a secret,
    the redacted text is stored in ``record.exc_text`` and ``record.exc_info`` is
    cleared, so no handler can render the original again. Records whose
    traceback holds nothing to redact are left exactly as they were.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True  # never drop a record because redaction tripped
        redacted = redact_secrets(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = None
        try:
            _redact_record_exception(record)
        except Exception:
            # Rendering the traceback failed, so it cannot be checked: drop it
            # rather than let a handler print it unredacted.
            record.exc_info = None
            record.exc_text = "Traceback omitted: it could not be checked for secrets."
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


def _redact_record_exception(record: logging.LogRecord) -> None:
    exc_info = record.exc_info
    text = record.exc_text
    if not text and exc_info and exc_info[0] is not None:
        text = _TRACEBACK_FORMATTER.formatException(exc_info)
    if not text:
        return
    cleaned = redact_secrets(text)
    if cleaned != text:
        record.exc_text = cleaned
        record.exc_info = None


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach :class:`SecretRedactingFilter` to every handler of *logger* (root by default).

    Filters on a *handler* see every record it emits, including records
    propagated from child loggers; a filter on the logger itself would not.
    Idempotent: a handler that already has the filter is left alone. Levels,
    formatters and destinations are not touched. Only handlers present at call
    time are covered.
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in list(target.handlers):
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
