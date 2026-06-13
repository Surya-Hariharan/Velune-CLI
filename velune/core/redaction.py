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

REDACTION_PLACEHOLDER = "***REDACTED***"

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

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTION_PLACEHOLDER, text)

    return text


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from every record's rendered message.

    Rewrites ``record.msg`` to the fully-formatted, redacted string and clears
    ``record.args`` so downstream formatters don't re-expand the original
    (unredacted) arguments.
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
        return True
