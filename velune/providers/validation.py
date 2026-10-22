"""Provider validation engine — real API calls, human-friendly diagnostics.

Every ``validate_provider()`` call makes a live round-trip to the provider's
API so that invalid, expired, revoked, or rate-limited keys are caught before
being persisted to the encrypted credential store (``providers/keystore.py``,
``providers/crypto.py``). The store's own AES-256-GCM master key — not the
provider keys directly — is what the OS keyring (Windows Credential Manager /
macOS Keychain / Linux Secret Service) protects. A cheap structural pre-check
rejects obvious paste errors; the provider's own response decides everything
else.

What a result proves
--------------------
The statuses keep five questions apart that used to collapse into "invalid
key": is the key *saved*, is *authentication confirmed*, is the provider
*reachable*, is a *model available*, does *inference succeed*. Only
:attr:`ValidationStatus.OK` means the provider authenticated this key. Only the
statuses in :data:`KEY_REJECTING_STATUSES` say the key itself is bad. Every
other failure — timeout, outage, rate limit, permission on one endpoint, a
missing test model — is *inconclusive* about the key and must never be
reported, or persisted, as an invalid key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from velune._compat import StrEnum


class ValidationStatus(StrEnum):
    OK = "ok"
    INVALID_KEY = "invalid_key"
    EXPIRED_KEY = "expired_key"
    REVOKED_KEY = "revoked_key"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    MALFORMED_KEY = "malformed_key"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN_ERROR = "unknown_error"
    #: No credential validator exists for this provider (e.g. a self-hosted
    #: OpenAI-compatible endpoint). This is "can't check", not "check failed" —
    #: reporting it as UNKNOWN_ERROR made healthy local providers look broken.
    NOT_SUPPORTED = "not_supported"
    #: The request did not complete in time.
    TIMEOUT = "timeout"
    #: The provider answered with a 5xx — an outage, not a verdict on the key.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: The provider is reachable but the check could not prove the key works
    #: (e.g. NVIDIA's model listing does not require a key, and the test model
    #: used to confirm it was unavailable).
    UNCONFIRMED = "unconfirmed"


#: The only verdicts that are statements about the credential itself.
KEY_REJECTING_STATUSES: frozenset[ValidationStatus] = frozenset(
    {
        ValidationStatus.INVALID_KEY,
        ValidationStatus.EXPIRED_KEY,
        ValidationStatus.REVOKED_KEY,
    }
)


@dataclass
class ValidationResult:
    provider_id: str
    status: ValidationStatus
    message: str
    models: list[str] = field(default_factory=list)
    account_info: dict = field(default_factory=dict)
    #: HTTP status of the deciding response, when there was one.
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == ValidationStatus.OK

    @property
    def rejects_key(self) -> bool:
        """True only when the provider said this key is bad."""
        return self.status in KEY_REJECTING_STATUSES

    @property
    def inconclusive(self) -> bool:
        """Not OK, but says nothing against the key (worth saving and retrying)."""
        return (
            not self.ok and not self.rejects_key and self.status != ValidationStatus.MALFORMED_KEY
        )

    def human_message(self) -> str:
        """Return a single, user-facing diagnostic line."""
        if self.ok:
            n = len(self.models)
            return (
                f"{self.provider_id} — authenticated ({n} model{'s' if n != 1 else ''} available)"
            )
        return self.message


# Well-documented, stable key prefixes. Deliberately sparse — a provider left
# out here (no fixed public format, or one that changes) just skips the
# prefix check and falls through to the minimum-length check plus the real
# network round-trip. This is a cheap pre-filter, never a substitute for it:
# matching a prefix does not mean the key is valid, only that it is not
# obviously wrong. The UIs still offer to save a key that fails it (a
# gateway/proxy key may legitimately look different).
_KEY_PREFIXES: dict[str, tuple[str, ...]] = {
    "openai": ("sk-",),
    "anthropic": ("sk-ant-",),
    "groq": ("gsk_",),
    "openrouter": ("sk-or-",),
    "nvidia": ("nvapi-",),
    "xai": ("xai-",),
    "huggingface": ("hf_",),
    "deepseek": ("sk-",),
}

_MIN_KEY_LENGTH = 16


def _structural_issue(provider_id: str, api_key: str) -> str | None:
    """Cheap, provider-aware pre-check for an obviously malformed key.

    Runs before any network request. Catches what a bad paste produces (an
    empty field, embedded whitespace, a key far too short to be real, or a
    prefix that rules out the provider entirely) — never what only the
    provider's own API can determine. A key passing this check is not "valid",
    only "not obviously wrong"; the network round-trip in ``_VALIDATORS`` is
    still the only source of truth for OK.
    """
    key = api_key.strip()
    if not key:
        return "Key is empty."
    if any(ch.isspace() for ch in key):
        return "Key contains whitespace or line breaks — check what you pasted."
    if len(key) < _MIN_KEY_LENGTH:
        return f"Key is too short to be a real {provider_id} credential."
    prefixes = _KEY_PREFIXES.get(provider_id)
    if prefixes and not key.startswith(prefixes):
        want = " or ".join(f"'{p}'" for p in prefixes)
        return f"{provider_id} keys start with {want} — this one doesn't."
    return None


def _redact_key(text: str, api_key: str) -> str:
    """Strip *api_key* out of *text* before it can be shown or persisted.

    Validation messages do not stay in memory: ``keystore.mark_invalid`` writes
    them to ``credentials.json`` under ``last_error`` and provider status output
    echoes them back. Any message built from an exception is therefore treated
    as untrusted, since HTTP client libraries routinely include the request URL
    (and anything in it) in their error text.
    """
    if not api_key or len(api_key) < 8:
        return text
    return text.replace(api_key, "***redacted***")


# ---------------------------------------------------------------------------
# Shared response / exception classification
# ---------------------------------------------------------------------------


def _provider_detail(resp: Any) -> str:
    """The provider's own error message, sanitized and shortened."""
    try:
        body = resp.json()
    except Exception:
        try:
            text = resp.text
        except Exception:
            return ""
        body = None
    else:
        text = ""
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("status") or "")
        elif isinstance(err, str):
            message = err
        elif body.get("detail"):
            message = str(body["detail"])
        elif body.get("message"):
            message = str(body["message"])
    else:
        message = text
    from velune.core.redaction import redact_secrets

    return redact_secrets(" ".join(message.split()))[:200]


def _classify_status(provider_id: str, label: str, resp: Any) -> ValidationResult:
    """Map a non-success HTTP response to a result, with the right category."""
    code = resp.status_code
    detail = _provider_detail(resp)
    lowered = detail.lower()
    said = f" Provider said: {detail}" if detail else ""

    if code == 401:
        if "expired" in lowered:
            return ValidationResult(
                provider_id,
                ValidationStatus.EXPIRED_KEY,
                f"{label} API key has expired.",
                http_status=code,
            )
        if "revoked" in lowered or "deactivated" in lowered or "disabled" in lowered:
            return ValidationResult(
                provider_id,
                ValidationStatus.REVOKED_KEY,
                f"{label} API key was revoked.",
                http_status=code,
            )
        return ValidationResult(
            provider_id,
            ValidationStatus.INVALID_KEY,
            f"Authentication failed: {label} rejected this API key.",
            http_status=code,
        )
    if code == 403:
        return ValidationResult(
            provider_id,
            ValidationStatus.PERMISSION_DENIED,
            f"Access denied or insufficient permissions at {label} (HTTP 403).{said}",
            http_status=code,
        )
    if code == 404:
        return ValidationResult(
            provider_id,
            ValidationStatus.UNKNOWN_ERROR,
            f"{label} endpoint not found (HTTP 404) — check the configured base URL.",
            http_status=code,
        )
    if code == 408:
        return ValidationResult(
            provider_id,
            ValidationStatus.TIMEOUT,
            f"Connection to {label} timed out (HTTP 408).",
            http_status=code,
        )
    if code == 429:
        return ValidationResult(
            provider_id,
            ValidationStatus.RATE_LIMITED,
            f"Rate limit reached at {label}. Try again in a moment.",
            http_status=code,
        )
    if 500 <= code < 600:
        return ValidationResult(
            provider_id,
            ValidationStatus.PROVIDER_UNAVAILABLE,
            f"{label} is temporarily unavailable (HTTP {code}).",
            http_status=code,
        )
    return ValidationResult(
        provider_id,
        ValidationStatus.UNKNOWN_ERROR,
        f"{label} returned HTTP {code}.{said}",
        http_status=code,
    )


def _classify_exception(
    provider_id: str, label: str, exc: BaseException, api_key: str
) -> ValidationResult:
    """Map a transport/parse failure to a result. Never claims the key is bad."""
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return ValidationResult(
            provider_id, ValidationStatus.TIMEOUT, f"Connection to {label} timed out."
        )
    if isinstance(exc, ValueError):  # includes json.JSONDecodeError
        return ValidationResult(
            provider_id,
            ValidationStatus.UNKNOWN_ERROR,
            f"{label} returned a response Velune could not read.",
        )
    return ValidationResult(
        provider_id,
        ValidationStatus.NETWORK_ERROR,
        _redact_key(f"Could not connect to {label}: {exc}", api_key),
    )


def _ids(field_name: str = "id", list_key: str = "data") -> Callable[[Any], list[str]]:
    def parse(body: Any) -> list[str]:
        items = body if isinstance(body, list) else (body or {}).get(list_key, [])
        return [str(m[field_name]) for m in items if isinstance(m, dict) and m.get(field_name)]

    return parse


@dataclass(frozen=True)
class _ListSpec:
    """A provider whose key is checked by an authenticated model listing."""

    label: str
    url: str | Callable[[], str]
    auth: Callable[[str], dict[str, str]] = lambda key: {"Authorization": f"Bearer {key}"}
    parse: Callable[[Any], list[str]] = field(default_factory=_ids)
    params: dict[str, Any] | None = None
    #: Provider-specific reading of a non-200 response; None → generic mapping.
    special: Callable[[str, Any], ValidationResult | None] | None = None


async def _list_check(provider_id: str, spec: _ListSpec, api_key: str) -> ValidationResult:
    import httpx

    url = spec.url() if callable(spec.url) else spec.url
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=spec.auth(api_key), params=spec.params)
        if resp.status_code == 200:
            models = spec.parse(resp.json())
            return ValidationResult(
                provider_id=provider_id,
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
                http_status=200,
            )
        if spec.special is not None:
            special = spec.special(provider_id, resp)
            if special is not None:
                return special
        return _classify_status(provider_id, spec.label, resp)
    except Exception as e:
        return _classify_exception(provider_id, spec.label, e, api_key)


# ---------------------------------------------------------------------------
# Provider-specific readings
# ---------------------------------------------------------------------------


def _google_special(provider_id: str, resp: Any) -> ValidationResult | None:
    try:
        err = (resp.json() or {}).get("error", {}) or {}
    except Exception:
        return None
    if resp.status_code == 400 and err.get("status") == "INVALID_ARGUMENT":
        return ValidationResult(
            provider_id,
            ValidationStatus.INVALID_KEY,
            "Authentication failed: invalid Google API key.",
            http_status=400,
        )
    if resp.status_code == 403 and "api key" in str(err.get("message", "")).lower():
        return ValidationResult(
            provider_id,
            ValidationStatus.INVALID_KEY,
            "Authentication failed: invalid or disabled Google API key.",
            http_status=403,
        )
    return None


def _anthropic_models(body: Any) -> list[str]:
    return _ids()(body)


def _openai_url() -> str:
    from velune.providers.endpoints import provider_base_url

    return f"{provider_base_url('openai')}/models"


def _anthropic_url() -> str:
    from velune.providers.endpoints import provider_base_url

    return f"{provider_base_url('anthropic')}/v1/models"


def _google_models(body: Any) -> list[str]:
    return [
        str(m.get("name", "")).split("/")[-1]
        for m in (body or {}).get("models", [])
        if isinstance(m, dict) and m.get("name")
    ]


_LIST_SPECS: dict[str, _ListSpec] = {
    "openai": _ListSpec("OpenAI", _openai_url),
    "anthropic": _ListSpec(
        # GET /v1/models is free (no tokens billed) and independent of any one
        # model id staying available, unlike a 1-token completion against a
        # hardcoded model that turns a valid key into a 404 once it is retired.
        "Anthropic",
        _anthropic_url,
        auth=lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"},
        parse=_anthropic_models,
        params={"limit": 1000},
    ),
    # The key goes in a header, never the query string. httpx embeds the
    # request URL in its exception messages, and those messages are persisted
    # to credentials.json via mark_invalid(reason=...) — a key in the URL would
    # be written to disk and echoed by provider status output.
    "google": _ListSpec(
        "Google",
        "https://generativelanguage.googleapis.com/v1beta/models",
        auth=lambda key: {"x-goog-api-key": key},
        parse=_google_models,
        special=_google_special,
    ),
    "groq": _ListSpec("Groq", "https://api.groq.com/openai/v1/models"),
    "openrouter": _ListSpec("OpenRouter", "https://openrouter.ai/api/v1/models"),
    "together": _ListSpec("Together.AI", "https://api.together.xyz/v1/models"),
    "fireworks": _ListSpec("Fireworks.AI", "https://api.fireworks.ai/inference/v1/models"),
    "deepseek": _ListSpec("DeepSeek", "https://api.deepseek.com/v1/models"),
    "mistral": _ListSpec("Mistral", "https://api.mistral.ai/v1/models"),
    "cohere": _ListSpec(
        "Cohere",
        "https://api.cohere.com/v1/models",
        parse=_ids(field_name="name", list_key="models"),
    ),
    "xai": _ListSpec("xAI", "https://api.x.ai/v1/models"),
    "meta": _ListSpec("Meta Llama API", "https://api.llama.com/compat/v1/models"),
}


def _make_list_validator(provider_id: str) -> Callable[[str], Any]:
    spec = _LIST_SPECS[provider_id]

    async def _validate(api_key: str) -> ValidationResult:
        return await _list_check(provider_id, spec, api_key)

    _validate.__name__ = f"_validate_{provider_id}"
    return _validate


_validate_openai = _make_list_validator("openai")
_validate_anthropic = _make_list_validator("anthropic")
_validate_google = _make_list_validator("google")
_validate_groq = _make_list_validator("groq")
_validate_openrouter = _make_list_validator("openrouter")
_validate_together = _make_list_validator("together")
_validate_fireworks = _make_list_validator("fireworks")
_validate_deepseek = _make_list_validator("deepseek")
_validate_mistral = _make_list_validator("mistral")
_validate_cohere = _make_list_validator("cohere")
_validate_xai = _make_list_validator("xai")
_validate_meta = _make_list_validator("meta")


async def _validate_huggingface(api_key: str) -> ValidationResult:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://huggingface.co/api/whoami-v2",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return ValidationResult(
                provider_id="huggingface",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=[],
                account_info={
                    "username": data.get("name", "unknown"),
                    "type": data.get("type", ""),
                },
                http_status=200,
            )
        return _classify_status("huggingface", "HuggingFace", resp)
    except Exception as e:
        return _classify_exception("huggingface", "HuggingFace", e, api_key)


async def _validate_nvidia(api_key: str) -> ValidationResult:
    """List NIM models, then confirm the key with a 1-token completion.

    NVIDIA's ``GET /v1/models`` answers 200 without checking the key, so a
    listing alone "verified" any ``nvapi-`` string and the failure only showed
    up at the first chat. The completion (``max_tokens=1``) against a small
    model that serves the hosted catalog is what actually exercises the key.
    If no test model can be called, the result is UNCONFIRMED — saved, but not
    claimed as verified.
    """
    import httpx

    from velune.providers.adapters.nvidia import pick_probe_model
    from velune.providers.endpoints import provider_base_url

    label = "NVIDIA NIM"
    base = provider_base_url("nvidia")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            listing = await client.get(f"{base}/models", headers=headers)
        if listing.status_code != 200:
            return _classify_status("nvidia", label, listing)
        models = _ids()(listing.json())
    except Exception as e:
        return _classify_exception("nvidia", label, e, api_key)

    probe = pick_probe_model(models)
    if probe is None:
        return ValidationResult(
            "nvidia",
            ValidationStatus.UNCONFIRMED,
            "NVIDIA NIM is reachable, but no chat model was listed to confirm the key with.",
            models=models,
        )
    try:
        # Cold-starting NIM functions can take a while to answer.
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": probe,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
    except Exception as e:
        result = _classify_exception("nvidia", label, e, api_key)
        result.models = models
        return result

    if resp.status_code == 200:
        return ValidationResult(
            provider_id="nvidia",
            status=ValidationStatus.OK,
            message="Authenticated successfully",
            models=models,
            account_info={"model_count": len(models), "verified_with": probe},
            http_status=200,
        )
    if resp.status_code in (404, 422):
        return ValidationResult(
            "nvidia",
            ValidationStatus.UNCONFIRMED,
            f"NVIDIA NIM is reachable, but the test model {probe} is unavailable "
            f"(HTTP {resp.status_code}), so the key could not be confirmed.",
            models=models,
            http_status=resp.status_code,
        )
    result = _classify_status("nvidia", label, resp)
    result.models = models
    return result


async def _validate_ollama(_api_key: str = "") -> ValidationResult:
    """Ollama is local and keyless — check the configured endpoint is serving."""
    import httpx

    from velune.providers.endpoints import ollama_base_url

    base = ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/api/tags")
        if resp.status_code == 200:
            data = resp.json()
            models = [
                m["name"] for m in data.get("models", []) if isinstance(m, dict) and "name" in m
            ]
            return ValidationResult(
                provider_id="ollama",
                status=ValidationStatus.OK,
                message=f"Ollama server reachable at {base}",
                models=models,
                account_info={"local": True, "model_count": len(models), "endpoint": base},
                http_status=200,
            )
        return _classify_status("ollama", f"Ollama at {base}", resp)
    except httpx.TimeoutException:
        return ValidationResult(
            "ollama", ValidationStatus.TIMEOUT, f"Connection to Ollama at {base} timed out."
        )
    except Exception:
        return ValidationResult(
            "ollama",
            ValidationStatus.NETWORK_ERROR,
            f"Ollama server is not reachable at {base}. Start it with `ollama serve`, "
            "or set OLLAMA_HOST to where it runs.",
        )


async def _validate_lmstudio(_api_key: str = "") -> ValidationResult:
    """LM Studio is local and keyless — just check reachability."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://localhost:1234/v1/models")
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="lmstudio",
                status=ValidationStatus.OK,
                message="LM Studio server reachable",
                models=models,
                account_info={"local": True, "model_count": len(models)},
            )
        return ValidationResult(
            "lmstudio",
            ValidationStatus.UNKNOWN_ERROR,
            f"LM Studio returned HTTP {resp.status_code}.",
        )
    except Exception:
        return ValidationResult(
            "lmstudio",
            ValidationStatus.NETWORK_ERROR,
            "LM Studio server is not running. Open LM Studio and start the server.",
        )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_VALIDATORS: dict[str, object] = {
    "openai": _validate_openai,
    "anthropic": _validate_anthropic,
    "google": _validate_google,
    "groq": _validate_groq,
    "openrouter": _validate_openrouter,
    "together": _validate_together,
    "fireworks": _validate_fireworks,
    "deepseek": _validate_deepseek,
    "mistral": _validate_mistral,
    "cohere": _validate_cohere,
    "nvidia": _validate_nvidia,
    "xai": _validate_xai,
    "meta": _validate_meta,
    "huggingface": _validate_huggingface,
    "ollama": _validate_ollama,
    "lmstudio": _validate_lmstudio,
}

#: Overall budget per provider. NVIDIA's check includes a completion that may
#: wait on a cold start.
_DEFAULT_BUDGET_S = 15.0
_BUDGETS_S: dict[str, float] = {"nvidia": 40.0}


async def validate_provider(provider_id: str, api_key: str = "") -> ValidationResult:
    """Validate *provider_id* credentials with a live API round-trip.

    Returns a :class:`ValidationResult` whose ``ok`` property is True only when
    authentication succeeded.  Human-readable diagnostics are always available
    via :meth:`ValidationResult.human_message`.
    """
    validator = _VALIDATORS.get(provider_id)
    if validator is None:
        return ValidationResult(
            provider_id=provider_id,
            status=ValidationStatus.NOT_SUPPORTED,
            message=(
                f"No credential check available for '{provider_id}' — "
                "configure its endpoint and Velune will use it directly."
            ),
        )
    if provider_id not in ("ollama", "lmstudio"):
        issue = _structural_issue(provider_id, api_key)
        if issue is not None:
            return ValidationResult(
                provider_id=provider_id, status=ValidationStatus.MALFORMED_KEY, message=issue
            )
    budget = _BUDGETS_S.get(provider_id, _DEFAULT_BUDGET_S)
    try:
        return await asyncio.wait_for(validator(api_key), timeout=budget)  # type: ignore[operator]
    except (asyncio.TimeoutError, TimeoutError):  # distinct classes before 3.11
        return ValidationResult(
            provider_id=provider_id,
            status=ValidationStatus.TIMEOUT,
            message=f"Validation timed out for {provider_id}. Check your network connection.",
        )


def validate_provider_sync(provider_id: str, api_key: str = "") -> ValidationResult:
    """Synchronous wrapper around :func:`validate_provider` for non-async contexts.

    Mirrors ``credential_manager.add_credential_sync``'s bridging strategy.
    Note this must **not** use ``asyncio.get_event_loop()``: since Python 3.12
    that raises ``RuntimeError`` in a thread with no current loop (rather than
    creating one), which is exactly the situation at a Typer/CLI entry point —
    the resulting error was being swallowed by the ``except`` below and
    reported to users as a bogus "Validation error: There is no current event
    loop" for every provider.
    """
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop running in this thread — the typical Typer/CLI entry
            # point. Bridge through the kernel's single blocking-entry helper.
            from velune.kernel.entrypoint import run_async

            return run_async(validate_provider(provider_id, api_key))
        # Called from inside a running loop: hand the work to a worker thread
        # that owns its own loop rather than blocking or re-entering this one.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, validate_provider(provider_id, api_key))
            return future.result(timeout=_BUDGETS_S.get(provider_id, _DEFAULT_BUDGET_S) + 5.0)
    except Exception as e:
        return ValidationResult(
            provider_id=provider_id,
            status=ValidationStatus.UNKNOWN_ERROR,
            message=f"Validation error: {e}",
        )


def supported_providers() -> list[str]:
    """Return provider IDs that have a validator registered."""
    return sorted(_VALIDATORS.keys())
