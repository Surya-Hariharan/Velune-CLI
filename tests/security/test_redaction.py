"""Secret-redaction regression tests.

Guards against credential leakage through logs: provider key shapes and live
env-var values must be scrubbed; ordinary log text must pass through intact.
"""

from __future__ import annotations

import logging

import pytest

from velune.core.redaction import (
    REDACTION_PLACEHOLDER,
    SecretRedactingFilter,
    redact_secrets,
)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-" + "A" * 40,
        "sk-proj-" + "B" * 40,
        "sk-" + "C" * 40,
        "xai-" + "D" * 40,
        "gsk_" + "E" * 40,
        "hf_" + "F" * 40,
        "AIza" + "G" * 35,
        "sk-or-" + "H" * 40,
    ],
)
def test_known_key_shapes_are_redacted(secret: str) -> None:
    out = redact_secrets(f"calling provider with key {secret} now")
    assert secret not in out
    assert REDACTION_PLACEHOLDER in out


def test_bearer_token_redacted() -> None:
    out = redact_secrets("Authorization: Bearer abcDEF123456ghijklmnop")
    assert "abcDEF123456ghijklmnop" not in out
    assert REDACTION_PLACEHOLDER in out


def test_env_var_value_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "totally-custom-gateway-key-9999")
    out = redact_secrets("request failed for key totally-custom-gateway-key-9999")
    assert "totally-custom-gateway-key-9999" not in out
    assert REDACTION_PLACEHOLDER in out


def test_ordinary_text_is_untouched() -> None:
    msg = "Skipped 3 turns; token budget 12000; sk- is a prefix but short"
    assert redact_secrets(msg) == msg


def test_logging_filter_scrubs_record() -> None:
    record = logging.LogRecord(
        name="velune.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth header was %s",
        args=("Bearer abcDEF123456ghijklmnop",),
        exc_info=None,
    )
    SecretRedactingFilter().filter(record)
    rendered = record.getMessage()
    assert "abcDEF123456ghijklmnop" not in rendered
    assert REDACTION_PLACEHOLDER in rendered
