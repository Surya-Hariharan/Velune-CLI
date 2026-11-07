"""Read-only lookup of published Velune releases from PyPI's JSON metadata API.

Index policy (what this does and does not do)
--------------------------------------------
* One fixed source: ``https://pypi.org/pypi/velune-cli/json``. The package name
  is a constant, never user input.
* Only metadata is read. No wheel or sdist is downloaded, and nothing from the
  response is executed or imported.
* Proxy and CA settings follow httpx's defaults (``HTTPS_PROXY``,
  ``SSL_CERT_FILE``, ...). Redirects are not followed: PyPI answers this URL
  directly, so a redirect means something unexpected is in the way.
* The response must say it describes ``velune-cli`` (a guard against a
  different project, or a mirror answering for another name).
* The answer is bounded (time and size) and validated structurally; nothing is
  guessed from a malformed reply.
* **Limits:** PyPI's JSON API is not served by arbitrary indexes. If uv is
  configured to install from another index, this check still reports PyPI's view
  (see :func:`index_override_warnings`). TLS and PyPI are trusted as-is: there is
  no signature or attestation check, because none is published yet.

Error text never includes the underlying exception message, which can carry
URLs, proxy credentials or local paths; only the exception *kind* is reported.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from velune import __version__
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import DISTRIBUTION, normalize_name

PYPI_JSON_URL = f"https://pypi.org/pypi/{DISTRIBUTION}/json"
INDEX_HOST = "pypi.org"
DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Settings that can make uv/pip install from somewhere other than PyPI.
_INDEX_ENV_VARS = (
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX",
    "UV_EXTRA_INDEX_URL",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
)

_RETRY = "Check your network connection and proxy settings, then try again."


def _network_error(reason: str, message: str, **details: Any) -> LifecycleError:
    return LifecycleError(
        ExitCode.NETWORK,
        f"{message} Nothing was changed.",
        recovery=_RETRY,
        details={"reason": reason, **details},
    )


def index_override_warnings(environ: Mapping[str, str]) -> list[str]:
    """Warn (naming the variables, never their values) when another index may be in play."""
    names = [name for name in _INDEX_ENV_VARS if environ.get(name)]
    if not names:
        return []
    return [
        f"{', '.join(names)} is set, so uv may install from a different package index. "
        f"This check only asks {INDEX_HOST}."
    ]


def _retry_after(response: httpx.Response) -> str | None:
    value = response.headers.get("retry-after", "").strip()
    return value if value.isdigit() and len(value) <= 6 else None


def fetch_releases(
    *,
    url: str = PYPI_JSON_URL,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Return the ``releases`` mapping for velune-cli, or raise a NETWORK ``LifecycleError``."""
    headers = {
        "Accept": "application/json",
        "User-Agent": f"velune-cli/{__version__} (update check)",
    }
    limits = httpx.Timeout(timeout, connect=min(timeout, 5.0))
    body = bytearray()
    try:
        with (
            httpx.Client(
                timeout=limits, headers=headers, follow_redirects=False, transport=transport
            ) as client,
            client.stream("GET", url) as response,
        ):
            status = response.status_code
            if status == 429:
                after = _retry_after(response)
                raise _network_error(
                    "rate_limited",
                    f"{INDEX_HOST} is rate limiting requests (HTTP 429)"
                    + (f"; it asked to wait {after} seconds." if after else "."),
                    status=429,
                )
            if status != 200:
                raise _network_error(
                    "http_status", f"{INDEX_HOST} answered with HTTP {status}.", status=status
                )
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise _network_error(
                        "response_too_large", f"The reply from {INDEX_HOST} was unexpectedly large."
                    )
    except LifecycleError:
        raise
    except httpx.TimeoutException as exc:
        raise _network_error(
            "timeout",
            f"Timed out waiting for {INDEX_HOST}.",
            error_type=type(exc).__name__,
        ) from exc
    except httpx.TransportError as exc:
        raise _network_error(
            "connection_failed",
            f"Could not connect to {INDEX_HOST}.",
            error_type=type(exc).__name__,
        ) from exc
    except httpx.HTTPError as exc:
        raise _network_error(
            "http_error", f"The request to {INDEX_HOST} failed.", error_type=type(exc).__name__
        ) from exc

    try:
        data = json.loads(bytes(body))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise _network_error(
            "invalid_json", f"{INDEX_HOST} returned a reply that is not valid JSON."
        ) from exc
    return _validated_releases(data)


def _validated_releases(data: Any) -> dict[str, Any]:
    def malformed(what: str) -> LifecycleError:
        return _network_error(
            "unexpected_structure", f"{INDEX_HOST} returned an unexpected reply ({what})."
        )

    if not isinstance(data, dict):
        raise malformed("not an object")
    info = data.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("name"), str):
        raise malformed("no package name")
    if normalize_name(info["name"]) != DISTRIBUTION:
        raise malformed("it describes a different package")
    releases = data.get("releases")
    if not isinstance(releases, dict):
        raise malformed("no release list")
    if not all(isinstance(key, str) for key in releases):
        raise malformed("bad release list")
    return releases
