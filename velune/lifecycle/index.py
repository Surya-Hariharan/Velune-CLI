"""Read-only lookup of published Velune releases from PyPI's Simple JSON API.

Index policy (what this does and does not do)
--------------------------------------------
* One fixed source: ``https://pypi.org/simple/velune-cli/``, requested as
  ``application/vnd.pypi.simple.v1+json`` (PEP 691 / PEP 700; PyPI's Index API).
  The package name is a constant, never user input. The deprecated ``releases``
  field of the ``/pypi/<name>/json`` API is deliberately not used, and there is
  no fallback to it.
* Only metadata is read. No wheel or sdist is downloaded, and nothing from the
  response is executed or imported.
* The reply must be JSON of the expected media type, declare an API version with
  major number 1 (PEP 629: minor versions only add fields), and say it describes
  ``velune-cli``.
* Each file's version comes from its *filename* parsed with
  ``packaging.utils.parse_wheel_filename`` / ``parse_sdist_filename``; filenames
  those helpers reject, or that name another project, are ignored. Files of the
  same version are merged into one entry, so a version is one candidate however
  many files it has.
* Proxy and CA settings follow httpx's defaults (``HTTPS_PROXY``,
  ``SSL_CERT_FILE``, ...). Redirects are not followed: PyPI answers this URL
  directly, so a redirect means something unexpected is in the way.
* The answer is bounded (time and size) and validated structurally; nothing is
  guessed from a malformed reply.
* **Limits:** if uv is configured to install from another index, this check still
  reports PyPI's view (see :func:`index_override_warnings`). TLS and PyPI are
  trusted as-is: there is no signature or attestation check, because none is
  published yet.

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

PYPI_SIMPLE_URL = f"https://pypi.org/simple/{DISTRIBUTION}/"
SIMPLE_JSON_TYPE = "application/vnd.pypi.simple.v1+json"
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
    url: str = PYPI_SIMPLE_URL,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Return ``{version: [{"yanked": bool, "requires_python": str | None}, ...]}``.

    Raises a NETWORK ``LifecycleError`` if the index cannot be read or answers
    with anything unexpected.
    """
    headers = {
        "Accept": SIMPLE_JSON_TYPE,
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
            media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if media_type != SIMPLE_JSON_TYPE:
                raise _network_error(
                    "unsupported_content_type",
                    f"{INDEX_HOST} did not answer in the expected format.",
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
    return _releases_from_simple(data)


def _file_version(filename: str, parse_wheel: Any, parse_sdist: Any) -> Any | None:
    """The ``Version`` of a velune-cli distribution filename, or ``None`` to ignore it."""
    try:
        if filename.endswith(".whl"):
            name, version, _build, _tags = parse_wheel(filename)
        elif filename.endswith((".tar.gz", ".zip")):
            name, version = parse_sdist(filename)
        else:
            return None
    except ValueError:  # InvalidWheelFilename / InvalidSdistFilename / InvalidVersion
        return None
    return version if normalize_name(str(name)) == DISTRIBUTION else None


def _releases_from_simple(data: Any) -> dict[str, Any]:
    def malformed(what: str) -> LifecycleError:
        return _network_error(
            "unexpected_structure", f"{INDEX_HOST} returned an unexpected reply ({what})."
        )

    if not isinstance(data, dict):
        raise malformed("not an object")
    meta = data.get("meta")
    api_version = meta.get("api-version") if isinstance(meta, dict) else None
    if not isinstance(api_version, str):
        raise malformed("no API version")
    if api_version.split(".")[0] != "1":
        raise malformed("unsupported API version")
    name = data.get("name")
    if not isinstance(name, str):
        raise malformed("no package name")
    if normalize_name(name) != DISTRIBUTION:
        raise malformed("it describes a different package")
    files = data.get("files")
    if not isinstance(files, list):
        raise malformed("no file list")

    try:
        from packaging.utils import parse_sdist_filename, parse_wheel_filename
    except ImportError as exc:  # same condition versions.load_version_tools reports
        raise LifecycleError(
            ExitCode.ERROR,
            "Velune cannot read version numbers because the 'packaging' library is missing "
            "or damaged in this Python environment. Nothing was changed.",
            details={"reason": "version_library_unavailable", "error_type": type(exc).__name__},
        ) from exc

    # Keyed by Version, not by text: 1.0 and 1.0.0 are the same release (PEP 440).
    releases: dict[Any, list[dict[str, Any]]] = {}
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
            continue
        version = _file_version(entry["filename"], parse_wheel_filename, parse_sdist_filename)
        if version is None:
            continue
        yanked = entry.get("yanked", False)
        releases.setdefault(version, []).append(
            {
                # PEP 691: a bool, or a string giving the reason (which means yanked).
                # Any other type is treated as yanked: when unsure, do not recommend it.
                "yanked": yanked if isinstance(yanked, bool) else True,
                "requires_python": entry.get("requires-python"),
            }
        )
    return {str(version): items for version, items in releases.items()}
