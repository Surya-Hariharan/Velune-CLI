"""The HTTP executor: one request, run safely (VAIR A3).

Everything that decides *whether* a request may go out lives in
:mod:`~velune.resources.http.netpolicy` and the permission gate. This module
decides *how* it goes out and what comes back:

* The connection goes through :class:`~velune.resources.http.transport.PinnedTransport`,
  so the address that is contacted is the address the policy validated.
* **Redirects are followed by this code, not by httpx.** Every hop is parsed
  strictly and classified again; a hop to a blocked destination, a downgrade to
  plain HTTP, a jump from a public host to a local one, or an unsafe method
  re-sent to a new origin is refused. Credentials, cookies and anything a
  credential injected are stripped whenever the origin changes.
* **Limits** — connect/read/write/pool timeouts, an overall deadline that covers
  every redirect hop, a cap on the request body, a cap on the response body that
  is applied while streaming (the rest is never read), a redirect cap and a
  connection cap.
* **Decompression is bounded.** Only gzip and deflate are advertised and
  accepted, and they are inflated with an output limit, so a compression bomb
  cannot allocate more than the response cap.
* **Nothing is left open.** The client and every response stream are closed on
  success, on failure and on cancellation.

Failures are raised as :class:`HttpFailure` with a normalized
:class:`~velune.resources.base.ErrorCode`; messages never include credentials.
"""

from __future__ import annotations

import asyncio
import re
import ssl
import time
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin

import httpx

from velune.resources.base import ErrorCode
from velune.resources.http.netpolicy import (
    Destination,
    NetworkPolicy,
    ParsedUrl,
    PolicyViolation,
    Resolver,
    parse_url,
)
from velune.resources.http.transport import ConnectObserver, PinnedTransport

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Headers a caller may not set: they would break message framing, redirect the
#: request to another virtual host, or enable a proxy path.
FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "expect",
        # Would change the method the server acts on after the method was judged.
        "x-http-method-override",
        "x-http-method",
        "x-method-override",
    }
)
#: Sent to a new origin never, whatever put them there.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_BAD_HEADER_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_MAX_HEADERS = 64
_MAX_HEADER_VALUE = 8192
_SUPPORTED_ENCODINGS = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})


class HttpFailure(Exception):
    """A request that did not produce a usable response."""

    def __init__(
        self, code: ErrorCode, message: str, *, hint: str = "", redirects: list | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.redirects = redirects or []


@dataclass(frozen=True, slots=True)
class HttpLimits:
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    write_timeout: float = 30.0
    pool_timeout: float = 5.0
    deadline: float = 30.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_request_bytes: int = 1024 * 1024
    max_redirects: int = 5
    max_connections: int = 10


@dataclass(slots=True)
class OutRequest:
    """A fully prepared request: strictly parsed URL, validated headers, body bytes."""

    method: str
    url: ParsedUrl
    headers: list[tuple[str, str]] = field(default_factory=list)
    #: Extra query pairs appended to the URL's own query (e.g. an API key).
    query: list[tuple[str, str]] = field(default_factory=list)
    body: bytes | None = None
    #: Header names / query names a credential injected (stripped on redirect).
    injected_headers: frozenset[str] = frozenset()
    follow_redirects: bool = True
    deadline: float | None = None
    verify_tls: bool = True


@dataclass(slots=True)
class HttpOutcome:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes
    url: str  # the final URL (query values not redacted here; the connector redacts)
    duration_ms: float
    request_id: str
    redirects: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None


def validate_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reject header names/values that could smuggle or break a request."""
    if len(headers) > _MAX_HEADERS:
        raise HttpFailure(ErrorCode.VALIDATION_FAILED, "Too many request headers.")
    clean: list[tuple[str, str]] = []
    for name, value in headers:
        if not _HEADER_NAME.match(name):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, f"'{name[:40]}' is not a valid header name."
            )
        if name.lower() in FORBIDDEN_HEADERS:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED,
                f"The '{name}' header is set by the client and cannot be overridden.",
            )
        if len(value) > _MAX_HEADER_VALUE or _BAD_HEADER_VALUE.search(value):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, f"The value of header '{name}' is not valid."
            )
        clean.append((name, value))
    return clean


def build_url(url: ParsedUrl, extra: list[tuple[str, str]]) -> str:
    """Re-assemble *url* (plus extra query pairs) from its validated parts."""
    query = url.query
    if extra:
        encoded = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in extra)
        query = f"{query}&{encoded}" if query else encoded
    host = f"[{url.host}]" if ":" in url.host else url.host
    default = {"http": 80, "https": 443}[url.scheme]
    port = "" if url.port == default else f":{url.port}"
    return f"{url.scheme}://{host}{port}{url.path}" + (f"?{query}" if query else "")


def _cause_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def map_exception(exc: BaseException, url: ParsedUrl, limits: HttpLimits) -> HttpFailure:
    """Normalize a transport exception into an :class:`HttpFailure` (no internals leaked)."""
    where = f"{url.host}:{url.port}"
    if isinstance(exc, PolicyViolation):
        code = {
            "dns_failed": ErrorCode.DNS_FAILED,
            "invalid_url": ErrorCode.INVALID_URL,
        }.get(exc.code, ErrorCode.NETWORK_POLICY)
        hint = {
            ErrorCode.DNS_FAILED: "Check the host name for typos and that DNS is reachable.",
            ErrorCode.NETWORK_POLICY: "Only trusted origins may reach local or private addresses; "
            "see the trusted_origins setting.",
        }.get(code, "")
        return HttpFailure(code, exc.message, hint=hint)
    if isinstance(exc, httpx.ConnectTimeout):
        return HttpFailure(
            ErrorCode.TIMEOUT,
            f"Connecting to {where} timed out after {limits.connect_timeout:g}s.",
            hint="The host may be down, filtered by a firewall, or not listening on that port.",
        )
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        phase = {"ReadTimeout": "reading the response", "WriteTimeout": "sending the request"}.get(
            type(exc).__name__, "waiting for a free connection"
        )
        return HttpFailure(
            ErrorCode.TIMEOUT,
            f"Timed out {phase} from {where}.",
            hint="The server accepted the connection but did not answer in time.",
        )
    if isinstance(exc, httpx.ConnectError):
        chain = _cause_chain(exc)
        if any(isinstance(c, ssl.SSLError) for c in chain):
            return HttpFailure(
                ErrorCode.TLS_FAILED,
                f"The TLS handshake with {url.host} failed: the certificate could not be verified "
                "(expired, untrusted issuer, or a name mismatch).",
                hint="Fix the server's certificate. Certificate checking is never disabled for the model.",
            )
        if any(isinstance(c, ConnectionRefusedError) for c in chain):
            return HttpFailure(
                ErrorCode.CONNECTION_FAILED,
                f"Connection refused by {where}.",
                hint="Nothing is listening there. Is the server running, and is the port right?",
            )
        return HttpFailure(
            ErrorCode.CONNECTION_FAILED,
            f"Could not connect to {where}.",
            hint="Check that the host is reachable from this machine.",
        )
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.CloseError)):
        return HttpFailure(ErrorCode.CONNECTION_FAILED, f"The connection to {where} was dropped.")
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.DecodingError)):
        return HttpFailure(
            ErrorCode.INVALID_RESPONSE, f"{where} sent a response that is not valid HTTP."
        )
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return HttpFailure(ErrorCode.INVALID_URL, "The URL could not be used.")
    if isinstance(exc, httpx.LocalProtocolError):
        return HttpFailure(ErrorCode.VALIDATION_FAILED, "The request could not be encoded as HTTP.")
    return HttpFailure(ErrorCode.CONNECTION_FAILED, f"The request to {where} failed.")


class _BoundedInflater:
    """Inflate gzip/deflate with a hard cap on the decoded size."""

    def __init__(self, encoding: str, limit: int) -> None:
        self._limit = limit
        self._produced = 0
        self._encoding = encoding
        self._decoder = zlib.decompressobj(31 if encoding in ("gzip", "x-gzip") else 15)
        self._raw_deflate_tried = False

    def feed(self, chunk: bytes) -> bytes:
        out = bytearray()
        pending = chunk
        while pending:
            room = self._limit - self._produced + 1  # one extra byte reveals overflow
            try:
                piece = self._decoder.decompress(pending, max(1, room))
            except zlib.error:
                if (
                    self._encoding == "deflate"
                    and not self._raw_deflate_tried
                    and not self._produced
                ):
                    # Some servers send raw deflate without the zlib header.
                    self._raw_deflate_tried = True
                    self._decoder = zlib.decompressobj(-15)
                    continue
                raise
            out += piece
            self._produced += len(piece)
            if self._produced > self._limit:
                return bytes(out)
            pending = self._decoder.unconsumed_tail
            if not piece and not pending:
                break
        return bytes(out)

    @property
    def overflowed(self) -> bool:
        return self._produced > self._limit


@dataclass(slots=True)
class HttpExecutor:
    """Run prepared requests under a :class:`NetworkPolicy`."""

    policy: NetworkPolicy
    limits: HttpLimits = field(default_factory=HttpLimits)
    resolver: Resolver | None = None
    on_connect: ConnectObserver | None = None
    #: Replace the transport (tests use ``httpx.MockTransport``). Never set in production.
    transport_factory: Callable[[bool], httpx.AsyncBaseTransport] | None = None
    #: A trust store for TLS verification (tests supply a throw-away CA).
    ssl_context: ssl.SSLContext | None = None
    user_agent: str = "velune-cli"

    def __post_init__(self) -> None:
        from velune.resources.http.present import install_log_redaction

        install_log_redaction()

    def _transport(self, verify_tls: bool) -> httpx.AsyncBaseTransport:
        if self.transport_factory is not None:
            return self.transport_factory(verify_tls)
        verify: bool | ssl.SSLContext = (
            self.ssl_context if self.ssl_context is not None else verify_tls
        )
        return PinnedTransport(
            self.policy,
            verify=verify,
            limits=httpx.Limits(
                max_connections=self.limits.max_connections,
                max_keepalive_connections=min(5, self.limits.max_connections),
                keepalive_expiry=5.0,
            ),
            resolver=self.resolver,
            on_connect=self.on_connect,
        )

    async def send(self, request: OutRequest) -> HttpOutcome:
        deadline = min(request.deadline or self.limits.deadline, 300.0)
        try:
            return await asyncio.wait_for(self._send(request), deadline)
        except (asyncio.TimeoutError, TimeoutError):
            raise HttpFailure(
                ErrorCode.TIMEOUT,
                f"The request did not finish within {deadline:g}s (including any redirects).",
            ) from None

    async def _send(self, request: OutRequest) -> HttpOutcome:
        limits = self.limits
        if request.method not in METHODS:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, f"Unsupported method '{request.method[:12]}'."
            )
        if request.body is not None and len(request.body) > limits.max_request_bytes:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED,
                f"The request body is {len(request.body)} bytes; the limit is {limits.max_request_bytes}.",
            )
        headers = validate_headers(request.headers)
        request_id = uuid.uuid4().hex[:16]
        if not any(n.lower() == "x-request-id" for n, _ in headers):
            headers.append(("X-Request-Id", request_id))
        names = {n.lower() for n, _ in headers}
        if "user-agent" not in names:
            headers.append(("User-Agent", self.user_agent))
        if "accept-encoding" not in names:
            headers.append(("Accept-Encoding", "gzip, deflate"))
        timeout = httpx.Timeout(
            connect=limits.connect_timeout,
            read=limits.read_timeout,
            write=limits.write_timeout,
            pool=limits.pool_timeout,
        )
        started = time.perf_counter()
        redirects: list[dict[str, object]] = []
        warnings: list[str] = []
        method = request.method
        body = request.body
        url = request.url
        extra_query = list(request.query)
        injected = {n.lower() for n in request.injected_headers}
        first_destination = self.policy.classify(url)
        self._reject_blocked(first_destination, hop=0)

        async with httpx.AsyncClient(
            transport=self._transport(request.verify_tls),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for hop in range(limits.max_redirects + 1):
                target = build_url(url, extra_query)
                built = client.build_request(method, target, headers=headers, content=body)
                if built.url.host.strip("[]").lower() != url.host:
                    raise HttpFailure(ErrorCode.INVALID_URL, "The URL host changed during parsing.")
                try:
                    response = await client.send(built, stream=True)
                except HttpFailure:
                    raise
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    raise map_exception(exc, url, limits) from None
                try:
                    location = response.headers.get("location")
                    if (
                        response.status_code in _REDIRECT_STATUSES
                        and location
                        and request.follow_redirects
                    ):
                        step = {
                            "status": response.status_code,
                            "from": _display(url),
                            "to": None,
                        }
                        if hop >= limits.max_redirects:
                            redirects.append(
                                {**step, "followed": False, "reason": "too many redirects"}
                            )
                            raise HttpFailure(
                                ErrorCode.REDIRECT_REJECTED,
                                f"Stopped after {limits.max_redirects} redirects.",
                                redirects=redirects,
                            )
                        redirects.append(step)
                        try:
                            decision = self._next_hop(
                                url, location, response.status_code, method, step
                            )
                        except HttpFailure as failure:
                            failure.redirects = redirects
                            raise
                        if decision is None:  # not followed: hand the 3xx back
                            warnings.append(str(step["reason"]))
                            outcome = await self._read(
                                response, request_id, started, url, redirects, warnings, method
                            )
                            return outcome
                        next_url, method, drop_body = decision
                        if drop_body:
                            body = None
                            headers = [
                                (n, v)
                                for n, v in headers
                                if n.lower() not in ("content-type", "content-length")
                            ]
                        if next_url.origin != url.origin:
                            headers = [
                                (n, v)
                                for n, v in headers
                                if n.lower() not in _CREDENTIAL_HEADERS
                                and n.lower() not in injected
                            ]
                            step["credentials_dropped"] = True
                        url = next_url
                        extra_query = []
                        continue
                    return await self._read(
                        response, request_id, started, url, redirects, warnings, method
                    )
                finally:
                    await response.aclose()
        raise HttpFailure(ErrorCode.REDIRECT_REJECTED, "Too many redirects.", redirects=redirects)

    @staticmethod
    def _reject_blocked(destination: Destination, *, hop: int) -> None:
        if destination.blocked:
            code = ErrorCode.REDIRECT_REJECTED if hop else ErrorCode.NETWORK_POLICY
            raise HttpFailure(code, destination.blocked.capitalize() + ".")

    def _next_hop(
        self,
        current: ParsedUrl,
        location: str,
        status: int,
        method: str,
        step: dict[str, object],
    ) -> tuple[ParsedUrl, str, bool] | None:
        """Decide a redirect: ``(next_url, method, drop_body)``, or None to stop (not followed)."""
        try:
            nxt = parse_url(urljoin(_display(current), location))
        except PolicyViolation as exc:
            raise HttpFailure(
                ErrorCode.REDIRECT_REJECTED, f"Redirect to an unusable URL: {exc.message}"
            ) from None
        step["to"] = _display(nxt)
        destination = self.policy.classify(nxt)
        if destination.blocked:
            step.update(followed=False, reason=destination.blocked)
            raise HttpFailure(
                ErrorCode.REDIRECT_REJECTED,
                f"Redirected to a destination that is not allowed: {destination.blocked}.",
            )
        new_method, drop_body = method, False
        if status == 303 and method != "HEAD":
            new_method, drop_body = "GET", True
        elif status in (301, 302) and method == "POST":
            new_method, drop_body = "GET", True
        if nxt.origin != current.origin:
            here = self.policy.classify(current)
            if current.scheme == "https" and nxt.scheme == "http" and not destination.local:
                step.update(followed=False, reason="redirect to plain HTTP refused (downgrade)")
                raise HttpFailure(
                    ErrorCode.REDIRECT_REJECTED,
                    "Refused a redirect from HTTPS to plain HTTP.",
                )
            if destination.local and not destination.trusted and not here.local:
                step.update(followed=False, reason="redirect from a public host to a local address")
                raise HttpFailure(
                    ErrorCode.REDIRECT_REJECTED,
                    "Refused a redirect from a public host to a local or private address.",
                )
            if new_method not in SAFE_METHODS:
                step.update(
                    followed=False,
                    reason=f"{method} request not re-sent to a different origin ({nxt.origin})",
                )
                return None
        step["followed"] = True
        return nxt, new_method, drop_body

    async def _read(
        self,
        response: httpx.Response,
        request_id: str,
        started: float,
        url: ParsedUrl,
        redirects: list[dict[str, object]],
        warnings: list[str],
        method: str,
    ) -> HttpOutcome:
        limit = self.limits.max_response_bytes
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in _SUPPORTED_ENCODINGS:
            raise HttpFailure(
                ErrorCode.INVALID_RESPONSE,
                f"The server used an unsupported content encoding ('{encoding[:20]}').",
            )
        declared = response.headers.get("content-length", "")
        too_large = HttpFailure(
            ErrorCode.RESPONSE_TOO_LARGE,
            f"HTTP {response.status_code} response body exceeds the {limit}-byte limit.",
            hint="Narrow the request (filters, pagination, a Range header) or raise max_response_bytes.",
            redirects=redirects,
        )
        if (
            method != "HEAD"
            and encoding in ("", "identity")
            and declared.isdigit()
            and int(declared) > limit
        ):
            raise too_large
        body = bytearray()
        try:
            if method == "HEAD":
                pass
            elif encoding in ("", "identity"):
                async for chunk in response.aiter_raw():
                    body += chunk
                    if len(body) > limit:
                        raise too_large
            else:
                inflater = _BoundedInflater(encoding, limit)
                async for chunk in response.aiter_raw():
                    try:
                        body += inflater.feed(chunk)
                    except zlib.error:
                        raise HttpFailure(
                            ErrorCode.INVALID_RESPONSE,
                            "The response body could not be decompressed.",
                        ) from None
                    if inflater.overflowed:
                        raise too_large
        except HttpFailure:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            raise map_exception(exc, url, self.limits) from None
        return HttpOutcome(
            status=response.status_code,
            reason=response.reason_phrase,
            headers=[(k, v) for k, v in response.headers.multi_items()][:100],
            body=bytes(body),
            url=_display(url),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            request_id=request_id,
            redirects=redirects,
            warnings=warnings,
        )


def _display(url: ParsedUrl) -> str:
    return build_url(url, [])


__all__ = [
    "METHODS",
    "SAFE_METHODS",
    "HttpExecutor",
    "HttpFailure",
    "HttpLimits",
    "HttpOutcome",
    "OutRequest",
    "build_url",
    "map_exception",
    "validate_headers",
]
