"""The Docker Engine connection (VAIR A4): endpoint selection, client lifecycle, errors.

Access to a Docker daemon is host-level privilege (anyone who can start a
container can mount the host's disk). This module is where that boundary is drawn:

* **Which daemon.** :func:`candidate_endpoints` is a pure function that turns
  configuration, ``DOCKER_HOST``, the active ``docker context`` and the platform
  default into an ordered list — a Windows named pipe, a Linux/macOS Unix socket
  (including rootless and Docker Desktop's per-user socket) — so connection
  selection is testable on any OS.
* **Never an unauthenticated network socket.** ``tcp://`` / ``http://`` endpoints
  are refused unless TLS with a CA and a client certificate is configured, and
  certificate verification is never turned off. ``ssh://`` uses the system ``ssh``
  client (its own host-key checking). A remote endpoint is flagged so every change
  to it is confirmed by a person.
* **Reachable, not installed.** Availability means ``ping`` succeeded and the API
  version is supported; the ``docker`` CLI being on ``PATH`` proves nothing.
* **Lifecycle.** One client is kept and reused. A connection error invalidates it;
  idempotent reads reconnect and retry once (a daemon restart), changes are never
  retried automatically. :meth:`DockerEngine.close` releases the client.

The SDK is imported lazily, so importing Velune never requires it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from velune.resources.base import ErrorCode

logger = logging.getLogger("velune.resources.docker.engine")

#: Oldest Engine API this integration is written against (Docker Engine 20.10).
MIN_API_VERSION = (1, 41)
WINDOWS_PIPES = ("npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine")


class EngineError(Exception):
    """A Docker failure normalized for the model (no secrets, bounded)."""

    def __init__(self, code: ErrorCode, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True, slots=True)
class TlsOptions:
    ca_cert: str
    client_cert: str
    client_key: str


@dataclass(frozen=True, slots=True)
class Endpoint:
    base_url: str
    source: str  # "config" | "DOCKER_HOST" | "context" | "default"
    remote: bool = False
    tls: TlsOptions | None = None
    ssh: bool = False

    def describe(self) -> str:
        """A secret-free label (scheme and host, never credentials or key paths)."""
        parsed = urlparse(self.base_url)
        if parsed.scheme in ("unix", "npipe"):
            return f"{parsed.scheme}:{parsed.path or parsed.netloc}"[:80]
        return f"{parsed.scheme}://{parsed.hostname or '?'}"


# ── Endpoint selection (pure) ───────────────────────────────────────────────


def read_context_endpoint(home: Path, environ: Mapping[str, str]) -> str | None:
    """The Host of the active ``docker context`` (None for the default context)."""
    try:
        name = environ.get("DOCKER_CONTEXT")
        if not name:
            config = json.loads((home / ".docker" / "config.json").read_text(encoding="utf-8"))
            name = config.get("currentContext")
        if not name or name == "default":
            return None
        digest = hashlib.sha256(str(name).encode()).hexdigest()
        meta = json.loads(
            (home / ".docker" / "contexts" / "meta" / digest / "meta.json").read_text(
                encoding="utf-8"
            )
        )
        host = ((meta.get("Endpoints") or {}).get("docker") or {}).get("Host")
        return str(host) if host else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def classify_endpoint(
    base_url: str,
    source: str,
    *,
    tls: TlsOptions | None,
    tls_verify_env: bool = False,
) -> Endpoint:
    """Validate *base_url* and return an :class:`Endpoint`, or raise :class:`EngineError`."""
    text = (base_url or "").strip()
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    if scheme == "unix" and parsed.path.startswith("/"):
        return Endpoint(text, source)
    if scheme == "npipe" and text.startswith("npipe:////./pipe/"):
        return Endpoint(text, source)
    if scheme == "ssh" and parsed.hostname:
        return Endpoint(text, source, remote=True, ssh=True)
    if scheme in ("tcp", "http", "https") and parsed.hostname:
        if tls is None:
            raise EngineError(
                ErrorCode.AUTHENTICATION_FAILED,
                f"Refused the unauthenticated Docker endpoint {scheme}://{parsed.hostname}: "
                "a network Docker socket gives root on that host.",
                "Use TLS: set resources.docker.tls_ca, tls_cert and tls_key (client-certificate "
                "authentication, verification stays on), or an ssh:// endpoint.",
            )
        return Endpoint(f"https://{parsed.netloc}", source, remote=True, tls=tls)
    raise EngineError(
        ErrorCode.VALIDATION_FAILED,
        f"Unsupported Docker endpoint '{scheme or text[:20]}'.",
        "Supported: unix://, npipe://, ssh:// and tcp:// with TLS.",
    )


def candidate_endpoints(
    config: Any,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
    runtime_dir: str | None = None,
    *,
    trust_config: bool = True,
) -> list[Endpoint | EngineError]:
    """Ordered endpoints to try; an unusable *explicit* choice appears as its error.

    Order: explicit configuration (only when *trust_config*: a hostile workspace's
    ``velune.toml`` may not point Docker somewhere else), ``DOCKER_HOST``, the active
    docker context, then the platform's default socket or pipe(s). An explicit choice
    never falls through to the defaults — a bad ``DOCKER_HOST`` is reported, not
    silently replaced.
    """
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = home or Path.home()
    tls = _tls_from(config, environ)
    out: list[Endpoint | EngineError] = []

    def explicit(url: str, source: str) -> list[Endpoint | EngineError]:
        try:
            verify_env = bool(environ.get("DOCKER_TLS_VERIFY"))
            return [classify_endpoint(url, source, tls=tls, tls_verify_env=verify_env)]
        except EngineError as exc:
            return [exc]

    configured = getattr(config, "host", None) if config is not None else None
    if configured and trust_config:
        return explicit(str(configured), "config")
    if environ.get("DOCKER_HOST"):
        return explicit(str(environ["DOCKER_HOST"]), "DOCKER_HOST")
    context = read_context_endpoint(home, environ)
    if context:
        return explicit(context, "context")
    if platform.startswith("win"):
        out += [Endpoint(p, "default") for p in WINDOWS_PIPES]
    else:
        out.append(Endpoint("unix:///var/run/docker.sock", "default"))
        if platform == "darwin":
            out.append(Endpoint(f"unix://{home}/.docker/run/docker.sock", "default"))
        runtime = runtime_dir if runtime_dir is not None else environ.get("XDG_RUNTIME_DIR")
        if runtime:
            out.append(Endpoint(f"unix://{runtime}/docker.sock", "default"))
        out.append(Endpoint(f"unix://{home}/.docker/desktop/docker.sock", "default"))
    return out


def _tls_from(config: Any, environ: Mapping[str, str]) -> TlsOptions | None:
    ca = getattr(config, "tls_ca", None) if config is not None else None
    cert = getattr(config, "tls_cert", None) if config is not None else None
    key = getattr(config, "tls_key", None) if config is not None else None
    if (
        not (ca and cert and key)
        and environ.get("DOCKER_TLS_VERIFY")
        and environ.get("DOCKER_CERT_PATH")
    ):
        base = Path(environ["DOCKER_CERT_PATH"])
        ca, cert, key = str(base / "ca.pem"), str(base / "cert.pem"), str(base / "key.pem")
    if ca and cert and key:
        return TlsOptions(str(ca), str(cert), str(key))
    return None


# ── Error normalization ─────────────────────────────────────────────────────


def _names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def map_error(exc: BaseException, what: str = "the Docker request") -> EngineError:
    """Normalize any SDK / transport exception (message bounded, never a traceback)."""
    from velune.core.redaction import redact_secrets

    if isinstance(exc, EngineError):
        return exc
    names = _names(exc)
    status = getattr(exc, "status_code", None)
    detail = ""
    for attr in ("explanation", "msg"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            detail = value
            break
    detail = redact_secrets(detail or str(exc))[:300]
    if "ImageNotFound" in names or "NotFound" in names or status == 404:
        return EngineError(ErrorCode.NOT_FOUND, f"Not found: {detail}")
    if status == 409 or "Conflict" in names:
        return EngineError(ErrorCode.CONFLICT, f"Conflict: {detail}")
    if status in (401, 403):
        return EngineError(
            ErrorCode.AUTHORIZATION_DENIED if status == 403 else ErrorCode.AUTHENTICATION_FAILED,
            f"The daemon refused {what}: {detail}",
        )
    if "ReadTimeout" in names or "Timeout" in names or isinstance(exc, TimeoutError):
        return EngineError(ErrorCode.TIMEOUT, f"{what[:60]} timed out.")
    if "ConnectionError" in names or isinstance(exc, (FileNotFoundError, ConnectionError, OSError)):
        return EngineError(
            ErrorCode.UNAVAILABLE,
            "The Docker daemon is not reachable.",
            "Start Docker Desktop (or the docker service) and try again.",
        )
    if "ImageLoadError" in names or "BuildError" in names:
        return EngineError(ErrorCode.ADAPTER_FAILURE, f"Docker could not complete {what}: {detail}")
    if "APIError" in names or "DockerException" in names:
        return EngineError(ErrorCode.ADAPTER_FAILURE, f"Docker could not complete {what}: {detail}")
    return EngineError(ErrorCode.ADAPTER_FAILURE, f"{what[:60]} failed ({type(exc).__name__}).")


def _connect_error(exc: BaseException) -> EngineError:
    """A failure while *reaching* the daemon is always "not reachable" (the SDK raises a
    bare ``DockerException`` for a missing socket or pipe)."""
    mapped = map_error(exc, "connecting")
    if mapped.code in (ErrorCode.AUTHORIZATION_DENIED, ErrorCode.AUTHENTICATION_FAILED):
        return mapped
    return EngineError(
        ErrorCode.UNAVAILABLE,
        "The Docker daemon is not running or not reachable.",
        "Start Docker Desktop (or the docker service) and try again.",
    )


def _is_connection_failure(error: EngineError) -> bool:
    return error.code is ErrorCode.UNAVAILABLE


def _parse_api(version: str) -> tuple[int, int]:
    try:
        major, minor, *_ = str(version).split(".")
        return int(major), int(minor)
    except ValueError:
        return (0, 0)


ClientFactory = Callable[[Endpoint, float], Any]


def sdk_client_factory(endpoint: Endpoint, timeout: float) -> Any:
    """Build a real ``docker.DockerClient`` (imports the SDK on first use)."""
    try:
        import docker
    except ImportError:
        raise EngineError(
            ErrorCode.UNAVAILABLE,
            "The Docker SDK for Python is not installed.",
            "Install it with: pip install 'velune-cli[docker]'",
        ) from None
    kwargs: dict[str, Any] = {"base_url": endpoint.base_url, "version": "auto", "timeout": timeout}
    if endpoint.tls is not None:
        kwargs["tls"] = docker.tls.TLSConfig(
            client_cert=(endpoint.tls.client_cert, endpoint.tls.client_key),
            ca_cert=endpoint.tls.ca_cert,
            verify=True,
        )
    if endpoint.ssh:
        kwargs["use_ssh_client"] = True
    return docker.DockerClient(**kwargs)


class DockerEngine:
    """One reusable client to one daemon, with reconnect-on-restart for reads."""

    def __init__(
        self,
        config: Any = None,
        *,
        client_factory: ClientFactory | None = None,
        environ: Mapping[str, str] | None = None,
        platform: str | None = None,
        home: Path | None = None,
        runtime_dir: str | None = None,
        trust_config: bool = True,
    ) -> None:
        self._config = config
        self._factory = client_factory or sdk_client_factory
        self._environ = environ
        self._platform = platform
        self._home = home
        self._runtime_dir = runtime_dir
        self._trust = trust_config
        self._timeout = float(getattr(config, "timeout", 20.0) or 20.0)
        self._lock = threading.RLock()
        self.client: Any = None
        self.endpoint: Endpoint | None = None
        self.server_version = ""
        self.api_version = ""
        self.last_error: EngineError | None = None
        self.connects = 0

    @property
    def connected(self) -> bool:
        return self.client is not None

    @property
    def remote(self) -> bool:
        return bool(self.endpoint and self.endpoint.remote)

    def candidates(self) -> list[Endpoint | EngineError]:
        return candidate_endpoints(
            self._config,
            self._environ,
            self._platform,
            self._home,
            self._runtime_dir,
            trust_config=self._trust,
        )

    # ── Connecting ───────────────────────────────────────────────────────────

    def connect_sync(self) -> dict[str, Any]:
        """Reach a daemon, verify the API version, keep the client. Raises :class:`EngineError`."""
        with self._lock:
            if self.client is not None:
                return self._summary()
            failures: list[EngineError] = []
            for candidate in self.candidates():
                if isinstance(candidate, EngineError):
                    self.last_error = candidate
                    raise candidate
                client = None
                try:
                    client = self._factory(candidate, self._timeout)
                    client.ping()
                    version = client.version()
                except EngineError as exc:
                    failures.append(exc)
                    if exc.code is ErrorCode.UNAVAILABLE and "SDK" in exc.message:
                        self.last_error = exc
                        raise
                    continue
                except Exception as exc:
                    failures.append(_connect_error(exc))
                    _close_quietly(client)
                    continue
                api = str(version.get("ApiVersion") or version.get("MinAPIVersion") or "")
                if _parse_api(api) < MIN_API_VERSION:
                    _close_quietly(client)
                    err = EngineError(
                        ErrorCode.UNAVAILABLE,
                        f"The Docker Engine API {api or 'unknown'} is older than the supported "
                        f"{MIN_API_VERSION[0]}.{MIN_API_VERSION[1]}.",
                        "Update Docker Engine / Docker Desktop.",
                    )
                    self.last_error = err
                    raise err
                self.client, self.endpoint = client, candidate
                self.server_version = str(version.get("Version") or "")
                self.api_version = api
                self.last_error = None
                self.connects += 1
                return self._summary()
            err = (
                failures[0]
                if failures
                else EngineError(ErrorCode.UNAVAILABLE, "No Docker endpoint to try.")
            )
            self.last_error = err
            raise err

    def _summary(self) -> dict[str, Any]:
        return {
            "version": self.server_version,
            "api_version": self.api_version,
            "endpoint": self.endpoint.describe() if self.endpoint else "",
            "remote": self.remote,
            "source": self.endpoint.source if self.endpoint else "",
        }

    def invalidate(self) -> None:
        with self._lock:
            client, self.client = self.client, None
        _close_quietly(client)

    def close(self) -> None:
        self.invalidate()
        self.endpoint = None

    # ── Calling ──────────────────────────────────────────────────────────────

    def call_sync(self, fn: Callable[[Any], Any], what: str, *, idempotent: bool = False) -> Any:
        """Run ``fn(client)``; on a lost connection reconnect and retry once if *idempotent*."""
        for attempt in (0, 1):
            self.connect_sync()
            try:
                return fn(self.client)
            except EngineError:
                raise
            except Exception as exc:
                error = map_error(exc, what)
                if _is_connection_failure(error):
                    self.invalidate()  # the daemon went away: do not reuse the client
                    if idempotent and attempt == 0:
                        continue
                raise error from None
        raise EngineError(ErrorCode.UNAVAILABLE, "The Docker daemon is not reachable.")

    async def call(self, fn: Callable[[Any], Any], what: str, *, idempotent: bool = False) -> Any:
        return await asyncio.to_thread(self.call_sync, fn, what, idempotent=idempotent)

    async def connect(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.connect_sync)

    async def ping(self) -> bool:
        """Is the daemon reachable *now*? (Never raises; drops a dead client.)"""
        try:
            await self.call(lambda c: c.ping(), "ping")
            return True
        except EngineError:
            return False


def _close_quietly(client: Any) -> None:
    try:
        if client is not None:
            client.close()
    except Exception:  # closing must never raise
        logger.debug("closing the Docker client failed", exc_info=True)
