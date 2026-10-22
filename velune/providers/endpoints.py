"""One resolver for where the Ollama daemon lives.

Before this module the answer depended on who asked. The chat adapter,
discovery and ``ollama_manager`` each re-implemented ``OLLAMA_HOST`` parsing;
the credential validator (which onboarding and ``/connect`` use) and the
liveness probe hardcoded ``localhost:11434``; and the provider registry always
handed the adapter the config default, so chat ignored ``OLLAMA_HOST`` while
discovery honoured it. A daemon on a non-default host or port was listed but
could not be validated, health-checked or chatted with.

Precedence, highest first:

1. An explicit ``[providers.ollama] base_url`` that differs from the default,
   published by :class:`~velune.providers.registry.ProviderRegistry` after its
   trust/egress checks (:func:`configure_ollama_url`).
2. ``OLLAMA_HOST``, parsed the way the Ollama CLI parses it.
3. ``http://localhost:11434``.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_OLLAMA_PORT = 11434

# Set by the provider registry from validated config; None means "no override".
_configured_ollama_url: str | None = None


def normalize_ollama_host(value: str) -> str:
    """Turn an ``OLLAMA_HOST``-style value into a client base URL.

    Accepts the forms Ollama itself accepts: ``host``, ``host:port``,
    ``:port``, and ``scheme://host[:port][/path]``. A missing port defaults to
    11434 over http (443 over https). The bind-all addresses ``0.0.0.0`` and
    ``::`` are valid for ``ollama serve`` but not as a destination on every
    platform, so they are mapped to loopback.
    """
    raw = value.strip().rstrip("/")
    if not raw:
        return DEFAULT_OLLAMA_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    host = parts.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else _DEFAULT_OLLAMA_PORT
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    path = parts.path.rstrip("/")
    return f"{scheme}://{host}:{port}{path}"


def configure_ollama_url(url: str | None) -> None:
    """Publish the validated config override (None clears it)."""
    global _configured_ollama_url
    url = (url or "").strip().rstrip("/")
    _configured_ollama_url = url if url and url != DEFAULT_OLLAMA_URL else None


def ollama_base_url() -> str:
    """The Ollama base URL every caller should use — see the module docstring."""
    if _configured_ollama_url:
        return _configured_ollama_url
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host:
        return normalize_ollama_host(host)
    return DEFAULT_OLLAMA_URL


# ---------------------------------------------------------------------------
# Cloud base URLs
#
# The registry is the only place that reads ``[providers.<id>] base_url`` and
# applies the workspace-trust and egress checks. It publishes the result here
# so validation and discovery hit the same endpoint inference does — before,
# a proxy/gateway base_url was used for chat but /v1/models and key
# validation still went to the vendor, where the gateway key was rejected.
# ---------------------------------------------------------------------------

DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}

_configured_base_urls: dict[str, str] = {}


def normalize_base_url(provider_id: str, url: str) -> str:
    """Strip trailing slashes, and a trailing ``/v1`` for Anthropic.

    Anthropic paths are built as ``{base}/v1/...``, so a base URL copied from
    docs as ``https://api.anthropic.com/v1`` used to produce ``/v1/v1/models``.
    """
    url = url.strip().rstrip("/")
    if provider_id == "anthropic" and url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def configure_base_url(provider_id: str, url: str | None) -> None:
    """Publish a validated base URL for *provider_id* (None/default clears it)."""
    default = DEFAULT_BASE_URLS.get(provider_id)
    if not url:
        _configured_base_urls.pop(provider_id, None)
        return
    normalized = normalize_base_url(provider_id, url)
    if default and normalized == normalize_base_url(provider_id, default):
        _configured_base_urls.pop(provider_id, None)
    else:
        _configured_base_urls[provider_id] = normalized


def provider_base_url(provider_id: str) -> str:
    """The base URL *provider_id* is actually used at (config override or default)."""
    configured = _configured_base_urls.get(provider_id)
    if configured:
        return configured
    return normalize_base_url(provider_id, DEFAULT_BASE_URLS.get(provider_id, ""))


def ollama_socket_address() -> tuple[str, int]:
    """``(host, port)`` of :func:`ollama_base_url`, for a raw TCP liveness probe."""
    parts = urlsplit(ollama_base_url())
    host = parts.hostname or "127.0.0.1"
    if host == "localhost":
        # Resolve to IPv4 loopback directly: on Windows "localhost" may try ::1
        # first and wait for the refusal before falling back.
        host = "127.0.0.1"
    try:
        port = parts.port
    except ValueError:
        port = None
    return host, port or (443 if parts.scheme == "https" else _DEFAULT_OLLAMA_PORT)
