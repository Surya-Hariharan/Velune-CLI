"""Secret-redaction regression tests."""

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
    output = redact_secrets(f"calling provider with key {secret} now")
    assert secret not in output
    assert REDACTION_PLACEHOLDER in output


def test_bearer_token_is_redacted() -> None:
    token = "abcDEF123456ghijklmnop"
    output = redact_secrets(f"Authorization: Bearer {token}")
    assert token not in output
    assert REDACTION_PLACEHOLDER in output


def test_environment_value_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "totally-custom-gateway-key-9999"
    monkeypatch.setenv("OPENAI_API_KEY", value)
    output = redact_secrets(f"request failed for key {value}")
    assert value not in output
    assert REDACTION_PLACEHOLDER in output


def test_ordinary_text_is_unchanged() -> None:
    message = "Skipped 3 turns; token budget 12000; sk- is a short prefix"
    assert redact_secrets(message) == message


def test_logging_filter_scrubs_formatted_record() -> None:
    token = "abcDEF123456ghijklmnop"
    record = logging.LogRecord(
        name="velune.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth header was %s",
        args=(f"Bearer {token}",),
        exc_info=None,
    )

    SecretRedactingFilter().filter(record)

    assert token not in record.getMessage()
    assert REDACTION_PLACEHOLDER in record.getMessage()
