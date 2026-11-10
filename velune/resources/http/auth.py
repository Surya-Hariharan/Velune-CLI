"""Credentials for the HTTP integration (VAIR A3).

Secrets live in Velune's existing encrypted keystore (the same AES-GCM store the
provider keys and database passwords use), under ``resource:http:<reference>``.
They are never written to ``velune.toml`` and never appear in anything the model
sees: configuration and requests carry only the **reference name**
(``aidly-dev-token``); the value is read at the moment a request is sent.

Every credential is **bound to one origin** (``https://api.example.com``). It is
attached only to a request for that origin, so a token issued for one API can
never be sent to another host — not by a mistyped URL, not by a redirect, not by
an OpenAPI document that names a different server. A mismatch is refused
loudly when the credential was asked for by name, and skipped with a warning when
it merely came from an environment's default.

Supported kinds:

``bearer``            ``Authorization: Bearer <token>``
``basic``             ``Authorization: Basic <base64(user:password)>``
``api_key_header``    a custom header (``X-API-Key``); name configurable
``api_key_query``     a query parameter. Works, but URLs end up in server logs
                      and browser history, so every use carries a warning
``oauth2_client_credentials``  fetch a token from the credential's own origin and
                      send it as a bearer token; cached until shortly before expiry
``secret``            a bare secret value, used for ``secret_variables``

OAuth2 authorization-code and device flows need a browser and a redirect
listener; they are not implemented.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from velune.resources.http.netpolicy import Origin, PolicyViolation, parse_origin

REF_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_QUERY_NAME = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,64}$")

AUTH_TYPES = (
    "bearer",
    "basic",
    "api_key_header",
    "api_key_query",
    "oauth2_client_credentials",
    "secret",
)
_REQUIRED: dict[str, tuple[str, ...]] = {
    "bearer": ("token",),
    "basic": ("username", "password"),
    "api_key_header": ("value",),
    "api_key_query": ("value",),
    "oauth2_client_credentials": ("token_url", "client_id", "client_secret"),
    "secret": ("value",),
}
_SECRET_FIELDS = ("token", "password", "value", "client_secret")
_TOKEN_SKEW_S = 30.0
_MAX_TOKEN_TTL_S = 3600.0


class CredentialError(Exception):
    """A credential problem. The message never contains a secret value."""


@dataclass(frozen=True, slots=True)
class Credential:
    ref: str
    kind: str
    origin: Origin | None
    fields: dict[str, Any] = field(repr=False)  # holds secrets: never shown

    def secret_values(self) -> list[str]:
        return [str(self.fields[k]) for k in _SECRET_FIELDS if self.fields.get(k)]


@dataclass(slots=True)
class Applied:
    """What a credential adds to one request."""

    headers: list[tuple[str, str]] = field(default_factory=list)
    query: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def header_names(self) -> set[str]:
        return {name.lower() for name, _ in self.headers}

    @property
    def query_names(self) -> set[str]:
        return {name for name, _ in self.query}


def valid_reference(ref: Any) -> bool:
    return isinstance(ref, str) and bool(REF_RE.match(ref))


def _register(values: list[str]) -> None:
    from velune.core.redaction import register_secret_value

    for value in values:
        register_secret_value(value)


def validate_credential(data: dict[str, Any]) -> dict[str, Any]:
    """Check a credential document; returns a cleaned copy or raises."""
    kind = data.get("type")
    if kind not in AUTH_TYPES:
        raise CredentialError(f"type must be one of: {', '.join(AUTH_TYPES)}")
    for name in _REQUIRED[kind]:
        value = data.get(name)
        if not isinstance(value, str) or not value:
            raise CredentialError(f"'{name}' is required for a {kind} credential")
    clean: dict[str, Any] = {"type": kind}
    for name in (*_REQUIRED[kind], "origin", "name", "scope"):
        if data.get(name):
            clean[name] = str(data[name])
    if kind != "secret":
        if not clean.get("origin"):
            raise CredentialError("'origin' is required: a credential is bound to one origin")
        try:
            clean["origin"] = str(parse_origin(clean["origin"]))
        except PolicyViolation as exc:
            raise CredentialError(f"'origin' is not a valid origin: {exc.message}") from None
    if kind == "api_key_header":
        clean["name"] = clean.get("name") or "X-API-Key"
        if not _HEADER_NAME.match(clean["name"]):
            raise CredentialError("'name' is not a valid header name")
    if kind == "api_key_query":
        clean["name"] = clean.get("name") or "api_key"
        if not _QUERY_NAME.match(clean["name"]):
            raise CredentialError("'name' is not a valid query parameter name")
    if kind == "oauth2_client_credentials":
        try:
            token_origin = parse_origin(clean["token_url"])
        except PolicyViolation:
            raise CredentialError("'token_url' is not a valid URL") from None
        if str(token_origin) != clean["origin"]:
            raise CredentialError("'token_url' must be on the credential's own origin")
    return clean


def save_credential(ref: str, data: dict[str, Any]) -> None:
    """Validate and store a credential (encrypted) under *ref*."""
    if not valid_reference(ref):
        raise CredentialError("the reference must be lowercase letters, digits, '.', '_' or '-'")
    clean = validate_credential(data)
    from velune.resources.secrets import save_resource_secret

    save_resource_secret("http", ref, clean)


def load_credential(ref: str) -> Credential:
    """Read a stored credential and register its secrets for redaction."""
    if not valid_reference(ref):
        raise CredentialError("that is not a valid credential reference")
    from velune.resources.secrets import load_resource_secret

    try:
        data = load_resource_secret("http", ref)
    except Exception:
        raise CredentialError(f"credential '{ref}' could not be read from the keystore") from None
    if data is None:
        raise CredentialError(
            f"credential '{ref}' is not stored. Save it with /resource configure http {ref}."
        )
    try:
        clean = validate_credential(data)
    except CredentialError as exc:
        raise CredentialError(f"credential '{ref}' is invalid: {exc}") from None
    credential = Credential(
        ref, clean["type"], parse_origin(clean["origin"]) if clean.get("origin") else None, clean
    )
    _register(credential.secret_values())
    return credential


def secret_value(ref: str) -> str:
    """The value of a ``secret`` credential (for a secret variable)."""
    credential = load_credential(ref)
    return str(credential.fields["value"])


def apply_credential(
    credential: Credential, origin: Origin, *, token: str | None = None
) -> Applied:
    """What *credential* contributes to a request to *origin*.

    Raises :class:`CredentialError` when the origin is not the one it is bound to.
    """
    if credential.kind == "secret":
        raise CredentialError(f"'{credential.ref}' is a secret value, not an authentication method")
    if credential.origin != origin:
        raise CredentialError(
            f"credential '{credential.ref}' is bound to {credential.origin} and is not sent to {origin}"
        )
    f = credential.fields
    applied = Applied()
    if credential.kind == "bearer":
        applied.headers.append(("Authorization", f"Bearer {f['token']}"))
    elif credential.kind == "basic":
        raw = f"{f['username']}:{f['password']}".encode()
        applied.headers.append(("Authorization", "Basic " + base64.b64encode(raw).decode("ascii")))
    elif credential.kind == "api_key_header":
        applied.headers.append((f["name"], f["value"]))
    elif credential.kind == "api_key_query":
        applied.query.append((f["name"], f["value"]))
        applied.warnings.append(
            "An API key is being sent in the URL query string, where servers, proxies and "
            "logs record it. Prefer an API-key header if the API allows it."
        )
    elif credential.kind == "oauth2_client_credentials":
        if not token:
            raise CredentialError(f"no access token is available for '{credential.ref}'")
        applied.headers.append(("Authorization", f"Bearer {token}"))
    return applied


# ── OAuth2 client credentials ───────────────────────────────────────────────

#: Posts a form to the credential's token endpoint: ``(url, form, basic_auth) ->
#: (status, json)``. Supplied by the executor so the request goes through the
#: same network policy and limits as everything else.
TokenFetcher = Callable[[str, dict[str, str], tuple[str, str]], Awaitable[tuple[int, Any]]]


@dataclass(slots=True)
class TokenCache:
    """In-memory OAuth2 access tokens, keyed by credential reference."""

    _tokens: dict[str, tuple[str, float]] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic

    def get(self, ref: str) -> str | None:
        entry = self._tokens.get(ref)
        if entry is None or self.clock() >= entry[1]:
            self._tokens.pop(ref, None)
            return None
        return entry[0]

    def put(self, ref: str, token: str, ttl: float) -> None:
        ttl = max(0.0, min(ttl, _MAX_TOKEN_TTL_S) - _TOKEN_SKEW_S)
        self._tokens[ref] = (token, self.clock() + ttl)

    def clear(self) -> None:
        self._tokens.clear()


async def oauth2_token(credential: Credential, cache: TokenCache, fetch: TokenFetcher) -> str:
    """A current access token for an ``oauth2_client_credentials`` credential."""
    cached = cache.get(credential.ref)
    if cached:
        return cached
    f = credential.fields
    form = {"grant_type": "client_credentials"}
    if f.get("scope"):
        form["scope"] = f["scope"]
    status, document = await fetch(f["token_url"], form, (f["client_id"], f["client_secret"]))
    if status != 200 or not isinstance(document, dict) or not document.get("access_token"):
        raise CredentialError(
            f"the token endpoint for '{credential.ref}' did not issue a token (HTTP {status})"
        )
    token = str(document["access_token"])
    _register([token])
    try:
        ttl = float(document.get("expires_in", 300))
    except (TypeError, ValueError):
        ttl = 300.0
    cache.put(credential.ref, token, ttl)
    return token


def describe_credential(credential: Credential) -> dict[str, Any]:
    """A secret-free summary (for listings)."""
    info: dict[str, Any] = {"reference": credential.ref, "type": credential.kind}
    if credential.origin is not None:
        info["origin"] = str(credential.origin)
    return info
