"""Redaction of integration credentials (VAIR A1)."""

from __future__ import annotations

import pytest

from velune.core import redaction
from velune.core.redaction import (
    REDACTION_PLACEHOLDER,
    is_sensitive_key,
    redact_secrets,
    redact_structure,
    register_secret_value,
    unregister_secret_value,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    with redaction._registry_lock:
        saved = set(redaction._registered)
        redaction._registered.clear()
    yield
    with redaction._registry_lock:
        redaction._registered.clear()
        redaction._registered.update(saved)


def test_registered_value_is_masked_regardless_of_shape():
    register_secret_value("plain-db-pass-42")
    out = redact_secrets("connected with plain-db-pass-42 ok")
    assert "plain-db-pass-42" not in out and REDACTION_PLACEHOLDER in out
    unregister_secret_value("plain-db-pass-42")
    assert "plain-db-pass-42" in redact_secrets("plain-db-pass-42")


def test_short_values_are_not_registered():
    register_secret_value("abc")  # would garble ordinary text
    assert redact_secrets("abc abc") == "abc abc"


def test_longest_registered_value_wins():
    register_secret_value("token-abcdef")
    register_secret_value("token-abcdef-extended")
    assert redact_secrets("x token-abcdef-extended y") == f"x {REDACTION_PLACEHOLDER} y"


def test_url_credentials_keep_the_url_readable():
    out = redact_secrets("dsn postgres://app:s3cretpw@localhost:5432/db")
    assert "s3cretpw" not in out
    assert "postgres://app:" in out and "@localhost:5432/db" in out


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter22",
        'PASSWORD: "hunter22"',
        "api_key=abcd1234efgh",
        '"client_secret": "zzzz-1111"',
        "host=db user=app pwd=hunter22",
    ],
)
def test_key_value_secrets_are_masked(text):
    out = redact_secrets(text)
    assert "hunter22" not in out and "abcd1234efgh" not in out and "zzzz-1111" not in out


def test_ordinary_text_is_untouched():
    for text in ("token budget exceeded", "the password field is required", "secret sauce"):
        assert redact_secrets(text) == text


@pytest.mark.parametrize(
    "key", ["password", "Authorization", "Set-Cookie", "x-api-key", "service_role_key", "DSN"]
)
def test_sensitive_keys(key):
    assert is_sensitive_key(key)


def test_redact_structure_masks_nested_sensitive_fields():
    data = {
        "status": 200,
        "headers": {"Authorization": "Bearer abc", "Content-Type": "application/json"},
        "body": [{"user": "a", "password": "p@ss-word-1"}, {"note": "password=topsecret1"}],
        "empty_token": "",
    }
    out = redact_structure(data)
    assert out["headers"]["Authorization"] == REDACTION_PLACEHOLDER
    assert out["headers"]["Content-Type"] == "application/json"
    assert out["body"][0]["password"] == REDACTION_PLACEHOLDER
    assert "topsecret1" not in out["body"][1]["note"]
    assert out["empty_token"] == ""
    assert data["body"][0]["password"] == "p@ss-word-1"  # input not mutated


def test_redact_structure_bounds_depth():
    deep: dict = {}
    node = deep
    for _ in range(100):
        node["x"] = {}
        node = node["x"]
    redact_structure(deep)  # must not recurse without limit


def test_loading_resource_secrets_registers_them(monkeypatch):
    from velune.resources import secrets

    monkeypatch.setattr(
        secrets,
        "get_key",
        lambda _id: (
            '{"host": "localhost", "password": "pg-pass-xyz", "url": "mysql://u:url-pass-123@h/db"}'
        ),
    )
    secrets.load_resource_secret("postgres", "default")
    out = redact_secrets("auth failed for pg-pass-xyz and url-pass-123")
    assert "pg-pass-xyz" not in out and "url-pass-123" not in out


def test_explicit_connector_config_is_registered():
    from velune.resources.postgres.connector import PostgresConnector

    PostgresConnector(config={"host": "localhost", "password": "explicit-pass-9"})._resolve_config()
    assert "explicit-pass-9" not in redact_secrets("explicit-pass-9")


def test_tool_loop_output_uses_the_registry():
    """The tool loop's existing redact_secrets call now covers integration secrets."""
    register_secret_value("loop-secret-value")
    assert "loop-secret-value" not in redact_secrets('{"result": "loop-secret-value"}')
