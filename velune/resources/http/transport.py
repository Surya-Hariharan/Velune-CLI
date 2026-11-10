"""An httpx transport that connects only to addresses the policy validated.

**The DNS-rebinding problem.** A client that validates ``api.example.com`` by
resolving it, and then hands the *name* to an HTTP library, lets the library
resolve it a second time. An attacker's DNS server answers the first query with a
public address and the second with ``127.0.0.1`` or a metadata address; the check
passed and the request went somewhere else.

**The fix used here.** The name is resolved exactly once, *inside the connection
attempt*, and the socket is opened to one of the addresses that were validated —
as an IP literal. There is no second lookup to race. Concretely:

* :class:`PinnedBackend` is an httpcore network backend. httpcore asks it to
  ``connect_tcp(host, port)`` with the **name** from the URL; it resolves the
  name, runs :meth:`NetworkPolicy.check_resolved` on the full answer and then
  connects to the validated address.
* Everything above the socket still sees the original name. httpcore performs
  the TLS handshake with ``server_hostname`` = the URL's host, so **SNI and
  certificate validation are against the real name**, and the ``Host`` header is
  the URL's host. Pinning changes where the bytes go, not who they claim to be
  talking to.
* Connection pooling is safe because a pooled connection is keyed by the origin
  *and was opened to a validated address*; reusing it never re-resolves. Each
  executor builds its own client, so a pool is never shared across requests that
  were judged separately.

The backend also refuses Unix sockets, so a request can never be routed to a
local socket path.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Iterable
from typing import Any

import httpcore
import httpx

from velune.resources.http.netpolicy import NetworkPolicy, PolicyViolation, Resolver

#: Called with ``(host, address, port)`` for every connection opened (tests and
#: tracing use it to prove which address a name was pinned to).
ConnectObserver = Callable[[str, str, int], None]


_CONTEXTS: dict[bool, ssl.SSLContext] = {}


def _shared_context(verify: bool) -> ssl.SSLContext:
    """One TLS context per verification mode: building it (loading the CA bundle) is slow."""
    context = _CONTEXTS.get(verify)
    if context is None:
        context = _CONTEXTS[verify] = httpx.create_ssl_context(verify)
    return context


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, validate, then connect to the validated address."""

    def __init__(
        self,
        policy: NetworkPolicy,
        *,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
        on_connect: ConnectObserver | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._inner = inner or httpcore.AnyIOBackend()
        self._on_connect = on_connect

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._policy.resolve(host, port, self._resolver)
        last_error: Exception | None = None
        for address in addresses:
            try:
                stream = await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc  # try the next validated address
                continue
            if self._on_connect is not None:
                self._on_connect(host, address, port)
            return stream
        assert last_error is not None  # check_resolved never returns an empty list
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise PolicyViolation("Unix sockets are not reachable from the HTTP integration.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """``httpx.AsyncHTTPTransport`` whose connection pool uses :class:`PinnedBackend`.

    HTTP/2 is off and proxies are never used: a proxy would resolve the name
    itself, which is exactly what this transport exists to prevent.
    """

    def __init__(
        self,
        policy: NetworkPolicy,
        *,
        verify: bool | ssl.SSLContext = True,
        limits: httpx.Limits | None = None,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
        on_connect: ConnectObserver | None = None,
    ) -> None:
        limits = limits or httpx.Limits(
            max_connections=10, max_keepalive_connections=5, keepalive_expiry=5.0
        )
        context = verify if isinstance(verify, ssl.SSLContext) else _shared_context(bool(verify))
        super().__init__(verify=context, trust_env=False, http1=True, http2=False, limits=limits)
        pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=PinnedBackend(
                policy, resolver=resolver, inner=inner, on_connect=on_connect
            ),
        )
        if not hasattr(self, "_pool"):  # an httpx without the attribute: refuse, never fall back
            raise RuntimeError("This httpx version is not supported by the pinned transport.")
        self._pool = pool
