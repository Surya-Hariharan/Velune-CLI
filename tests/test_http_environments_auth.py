"""Environments, variable interpolation and origin-bound credentials (VAIR A3)."""

from __future__ import annotations

import base64
import json

import pytest

from tests.http_fakes import isolate_keystore
from velune.core.redaction import redact_secrets
from velune.resources.http.auth import (
    CredentialError,
    TokenCache,
    apply_credential,
    describe_credential,
    load_credential,
    oauth2_token,
    save_credential,
    secret_value,
    valid_reference,
    validate_credential,
)
from velune.resources.http.environments import (
    Environment,
    VariableError,
    VariableScope,
    references,
)
from velune.resources.http.netpolicy import parse_origin

API = "https://api.example.com"


@pytest.fixture(autouse=True)
def keystore(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path)


def env(**overrides) -> Environment:
    config = {
        "base_url": "https://staging.example.com/api",
        "variables": {"api_version": "v1", "tenant": "acme"},
        "secret_variables": {"admin_token": "staging-admin"},
        "auth": "staging-token",
    }
    config.update(overrides)
    return Environment.from_config("staging", config)


# ── Environments ────────────────────────────────────────────────────────────


def test_an_environment_is_parsed_and_summarized_without_secrets():
    e = env()
    assert e.base_url == "https://staging.example.com/api"
    assert str(e.origin) == "https://staging.example.com"
    summary = e.summary()
    assert summary["variables"] == {"api_version": "v1", "tenant": "acme"}
    assert summary["secret_variables"] == ["admin_token"]  # names only
    assert summary["authentication"] == "credential reference configured"
    assert "staging-token" not in json.dumps(summary) and "staging-admin" not in json.dumps(summary)


def test_the_trailing_slash_of_a_base_url_is_normalized():
    assert env(base_url="http://localhost:5000/").base_url == "http://localhost:5000"


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": ""},
        {"base_url": "ftp://x"},
        {"base_url": "http://user:pass@host/"},
        {"variables": {"bad name": "x"}},
        {"variables": {"base_url": "x"}},  # shadows a built-in
        {"variables": {"a": "{{other}}"}},  # no recursion
        {"variables": {"a": "x" * 5000}},
        {"secret_variables": {"a": "Not A Ref"}},
        {"secret_variables": {"api_version": "ok-ref"}},  # defined twice
        {"auth": "Bad Ref!"},
        {"extra_origins": ["not a url"]},
    ],
)
def test_invalid_environments_are_rejected_with_a_clear_message(overrides):
    with pytest.raises(VariableError):
        env(**overrides)


def test_environment_names_are_validated():
    with pytest.raises(VariableError):
        Environment.from_config("bad name!", {"base_url": "http://x.test"})


def test_secrets_may_only_go_to_the_environments_own_origins():
    e = env(extra_origins=["https://auth.example.com"])
    assert e.origins == {
        parse_origin("https://staging.example.com"),
        parse_origin("https://auth.example.com"),
    }


# ── Interpolation ───────────────────────────────────────────────────────────


def test_variables_are_interpolated_including_the_builtin_base_url():
    scope = VariableScope(env())
    done = scope.interpolate("{{base_url}}/{{api_version}}/tenants/{{ tenant }}")
    assert done.text == "https://staging.example.com/api/v1/tenants/acme"
    assert done.used_secret is False


def test_an_unknown_variable_is_an_error_listing_available_names_not_values():
    with pytest.raises(VariableError) as caught:
        VariableScope(env()).interpolate("{{api_version}}/{{typo}}")
    message = str(caught.value)
    assert "typo" in message and "api_version" in message
    assert "acme" not in message  # values are not echoed


def test_without_an_environment_only_extra_variables_exist():
    scope = VariableScope(None, {"id": "7"})
    assert scope.interpolate("/users/{{id}}").text == "/users/7"
    with pytest.raises(VariableError):
        scope.interpolate("{{base_url}}/x")


def test_text_without_references_is_unchanged_and_braces_alone_are_not_references():
    assert (
        VariableScope(env()).interpolate("plain {text} {{ }} {single}").text
        == "plain {text} {{ }} {single}"
    )


def test_values_are_literal_and_not_re_interpolated():
    scope = VariableScope(env(), {"x": "{{api_version}}"})
    assert scope.interpolate("{{x}}").text == "{{api_version}}"


def test_references_lists_names_once_in_order():
    assert references("{{a}} {{b}} {{a}} {{ c }}") == ["a", "b", "c"]


def test_secret_variables_resolve_from_the_keystore_and_are_flagged():
    save_credential("staging-admin", {"type": "secret", "value": "s3cr3t-admin-value"})
    done = VariableScope(env()).interpolate("Bearer {{admin_token}}")
    assert done.text == "Bearer s3cr3t-admin-value" and done.used_secret is True
    assert redact_secrets(done.text) == "Bearer ***REDACTED***"  # registered for redaction


def test_a_missing_secret_variable_fails_without_naming_a_value():
    with pytest.raises(VariableError) as caught:
        VariableScope(env()).interpolate("{{admin_token}}")
    assert "staging-admin" in str(caught.value) and "not stored" in str(caught.value)


def test_extra_secret_variables_get_the_same_treatment():
    scope = VariableScope(env(), {"token": "abcdef-123456"}, extra_secrets={"token"})
    assert scope.interpolate("{{token}}").used_secret is True


def test_the_scope_names_include_secret_names_but_never_values():
    names = VariableScope(env(), {"extra": "1"}).names()
    assert {"base_url", "api_version", "tenant", "admin_token", "extra"} <= set(names)


# ── Credentials ─────────────────────────────────────────────────────────────


def test_credentials_are_stored_encrypted_and_never_in_plaintext_files(tmp_path):
    save_credential("my-api", {"type": "bearer", "origin": API, "token": "tok-plain-visible-1234"})
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"tok-plain-visible-1234" not in path.read_bytes()
    assert load_credential("my-api").fields["token"] == "tok-plain-visible-1234"


def test_loading_a_credential_registers_its_secrets_for_redaction():
    save_credential(
        "my-api", {"type": "basic", "origin": API, "username": "ada", "password": "pa55word-zzz"}
    )
    load_credential("my-api")
    assert "pa55word-zzz" not in redact_secrets("failed with pa55word-zzz here")


def test_descriptions_of_a_credential_contain_no_secret():
    save_credential("my-api", {"type": "bearer", "origin": API, "token": "tok-visible-secret-9"})
    shown = json.dumps(describe_credential(load_credential("my-api")))
    assert "tok-visible-secret-9" not in shown and "my-api" in shown and API in shown
    assert "tok-visible-secret-9" not in repr(load_credential("my-api"))


@pytest.mark.parametrize(
    "document",
    [
        {"type": "nope"},
        {"type": "bearer", "origin": API},  # no token
        {"type": "bearer", "token": "x"},  # no origin: unbound credentials are refused
        {"type": "bearer", "origin": "not a url", "token": "x"},
        {"type": "basic", "origin": API, "username": "u"},
        {"type": "api_key_header", "origin": API, "value": "k", "name": "Bad Name"},
        {"type": "api_key_query", "origin": API, "value": "k", "name": "bad name"},
        {
            "type": "oauth2_client_credentials",
            "origin": API,
            "token_url": "https://evil.example/t",
            "client_id": "i",
            "client_secret": "s",
        },  # a token endpoint on another origin
    ],
)
def test_invalid_credentials_are_rejected(document):
    with pytest.raises(CredentialError):
        validate_credential(document)


def test_references_are_validated():
    assert valid_reference("aidly-dev.token_1")
    assert (
        not valid_reference("Has Space")
        and not valid_reference("../x")
        and not valid_reference(None)
    )
    with pytest.raises(CredentialError):
        save_credential("../escape", {"type": "secret", "value": "abcdefgh"})
    with pytest.raises(CredentialError):
        load_credential("../escape")


def test_a_missing_credential_says_how_to_store_it():
    with pytest.raises(CredentialError) as caught:
        load_credential("never-saved")
    assert "/resource configure http never-saved" in str(caught.value)


def test_secret_value_requires_a_secret_credential_with_a_value():
    save_credential("only-a-secret", {"type": "secret", "value": "the-value-123"})
    assert secret_value("only-a-secret") == "the-value-123"


# ── Applying credentials ────────────────────────────────────────────────────


def apply(document, origin=API, **kw):
    save_credential("c", document)
    return apply_credential(load_credential("c"), parse_origin(origin), **kw)


def test_bearer():
    applied = apply({"type": "bearer", "origin": API, "token": "tok-123456"})
    assert applied.headers == [("Authorization", "Bearer tok-123456")]


def test_basic():
    applied = apply({"type": "basic", "origin": API, "username": "ada", "password": "pw-123456"})
    token = base64.b64decode(applied.headers[0][1].removeprefix("Basic ")).decode()
    assert token == "ada:pw-123456"


def test_api_key_header_uses_the_configured_name():
    applied = apply(
        {"type": "api_key_header", "origin": API, "name": "X-Custom-Key", "value": "key-123456"}
    )
    assert applied.headers == [("X-Custom-Key", "key-123456")]
    assert (
        apply({"type": "api_key_header", "origin": API, "value": "k-123456"}).headers[0][0]
        == "X-API-Key"
    )


def test_api_key_in_the_query_carries_a_security_warning():
    applied = apply(
        {"type": "api_key_query", "origin": API, "name": "apikey", "value": "key-123456"}
    )
    assert applied.query == [("apikey", "key-123456")] and applied.headers == []
    assert (
        "query string" in applied.warnings[0] and "Prefer an API-key header" in applied.warnings[0]
    )
    assert applied.query_names == {"apikey"}


@pytest.mark.parametrize(
    "target",
    [
        "https://other.example.com",  # unrelated host
        "http://api.example.com",  # downgraded scheme
        "https://api.example.com:8443",  # other port
        "https://api.example.com.evil.test",  # look-alike host
        "https://sub.api.example.com",  # subdomain
    ],
)
def test_a_credential_is_never_applied_to_another_origin(target):
    with pytest.raises(CredentialError) as caught:
        apply({"type": "bearer", "origin": API, "token": "tok-123456"}, origin=target)
    assert "is bound to" in str(caught.value) and "tok-123456" not in str(caught.value)


def test_a_bare_secret_is_not_an_authentication_method():
    with pytest.raises(CredentialError):
        apply({"type": "secret", "value": "abcdef123"})


# ── OAuth2 client credentials ───────────────────────────────────────────────

OAUTH = {
    "type": "oauth2_client_credentials",
    "origin": API,
    "token_url": f"{API}/oauth/token",
    "client_id": "client-1",
    "client_secret": "client-secret-xyz",
    "scope": "read",
}


async def test_an_access_token_is_fetched_cached_and_expires():
    save_credential("oauth", OAUTH)
    credential = load_credential("oauth")
    calls = []
    now = [0.0]

    async def fetch(url, form, basic):
        calls.append((url, form, basic))
        return 200, {"access_token": "access-token-abc123", "expires_in": 120}

    cache = TokenCache(clock=lambda: now[0])
    assert await oauth2_token(credential, cache, fetch) == "access-token-abc123"
    assert await oauth2_token(credential, cache, fetch) == "access-token-abc123"
    assert len(calls) == 1  # cached
    assert calls[0][1] == {"grant_type": "client_credentials", "scope": "read"}
    assert calls[0][2] == ("client-1", "client-secret-xyz")
    now[0] = 200.0  # past expiry (minus the safety margin)
    await oauth2_token(credential, cache, fetch)
    assert len(calls) == 2
    assert redact_secrets("leaked access-token-abc123") == "leaked ***REDACTED***"


async def test_a_token_endpoint_failure_does_not_leak_the_client_secret():
    save_credential("oauth", OAUTH)

    async def fetch(url, form, basic):
        return 401, {"error": "invalid_client"}

    with pytest.raises(CredentialError) as caught:
        await oauth2_token(load_credential("oauth"), TokenCache(), fetch)
    assert "client-secret-xyz" not in str(caught.value) and "401" in str(caught.value)


def test_applying_an_oauth_credential_needs_a_token():
    save_credential("oauth", OAUTH)
    with pytest.raises(CredentialError):
        apply_credential(load_credential("oauth"), parse_origin(API))
    applied = apply_credential(load_credential("oauth"), parse_origin(API), token="access-xyz")
    assert applied.headers == [("Authorization", "Bearer access-xyz")]
