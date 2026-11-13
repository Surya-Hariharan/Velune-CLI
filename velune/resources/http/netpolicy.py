"""Outbound network policy for the HTTP integration (VAIR A3).

Two questions are answered here, and they are answered at two different times:

* **Before a request runs** (:meth:`NetworkPolicy.classify`): *what kind of place
  is this URL, and how should the permission gate treat it?* This uses the URL
  text only and does no DNS, so it is cheap and deterministic. Its answer feeds
  the approval prompt (a loopback dev server is not the same as a public API).
* **When a connection is opened** (:meth:`NetworkPolicy.check_resolved`): *are
  the addresses this name really resolves to acceptable?* This is the check that
  matters for safety, and it is applied to the very addresses the socket is then
  connected to (see :mod:`velune.resources.http.transport`) — never to a
  separate lookup. That closes the DNS-rebinding gap of "validate, then let the
  client resolve again".

Address classes, strongest first:

``blocked``   Never reachable, whatever the configuration: cloud metadata
              services, link-local, unspecified, multicast and reserved ranges.
``loopback``  This machine. Reachable when the origin is listed as trusted, or
              when the URL itself names loopback (``localhost``, ``127.0.0.1``,
              ``[::1]``). A *public-looking name that resolves to loopback* is
              refused unless trusted — that is the rebinding shape.
``private``   RFC 1918 / ULA / shared-address space. Reachable only for trusted
              origins or when the policy opts in.
``public``    Everything else.

IPv6 transition addresses (IPv4-mapped, NAT64, 6to4, Teredo) are classified by
the IPv4 address they embed, so ``::ffff:127.0.0.1`` cannot slip past a rule
that blocks ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from velune._compat import StrEnum

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_URL_LENGTH = 8192
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
# A host made only of numeric labels is an IP address in some legacy notation
# (``127.1``, ``0x7f.0.0.1``, ``2130706433``, ``0177.0.0.1``). A real hostname
# always has a non-numeric top-level label, so rejecting these loses nothing.
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)$")
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Local services that are dangerous to speak HTTP to even on loopback (a Docker
#: engine on TCP is root on the host; databases and caches are not HTTP at all).
#: A request to one of these is HIGH risk unless the origin is explicitly trusted.
SENSITIVE_LOCAL_PORTS: dict[int, str] = {
    2375: "the Docker engine API",
    2376: "the Docker engine API",
    2379: "etcd",
    2380: "etcd",
    3306: "MySQL",
    5432: "PostgreSQL",
    6379: "Redis",
    6443: "the Kubernetes API server",
    9200: "Elasticsearch",
    10250: "the kubelet",
    11211: "memcached",
    27017: "MongoDB",
}

_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "169.254.169.254",  # AWS / Azure / GCP / most clouds
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud
        "fd00:ec2::254",  # AWS IPv6
    )
)
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.goog", "metadata"})

_ALWAYS_BLOCKED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network" / unspecified
        "169.254.0.0/16",  # link-local
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved + broadcast
    )
)
_ALWAYS_BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",  # unspecified
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_PRIVATE_EXTRA = (
    ipaddress.ip_network("100.64.0.0/10"),  # shared address space (CGNAT)
    ipaddress.ip_network("fc00::/7"),  # unique local addresses
)


class Kind(StrEnum):
    """What sort of place an address or URL is."""

    BLOCKED = "blocked"
    LOOPBACK = "loopback"
    PRIVATE = "private"
    PUBLIC = "public"


class PolicyViolation(Exception):
    """A destination the policy refuses. The message is safe to show the model."""

    def __init__(self, message: str, *, code: str = "network_policy_blocked") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True)
class Origin:
    """``scheme://host:port`` in canonical form (the unit credentials are bound to)."""

    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = _DEFAULT_PORTS.get(self.scheme)
        return f"{self.scheme}://{host}" + ("" if self.port == default else f":{self.port}")


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    """A URL that passed strict parsing, rebuilt from its parts."""

    scheme: str
    host: str  # lowercase, IDNA-encoded, no brackets, no trailing dot
    port: int
    path: str  # always starts with "/"
    query: str
    ip: IPAddress | None  # set when the host is an IP literal

    @property
    def origin(self) -> Origin:
        return Origin(self.scheme, self.host, self.port)

    @property
    def target(self) -> str:
        """Path plus query, as sent on the request line."""
        return self.path + (f"?{self.query}" if self.query else "")

    def __str__(self) -> str:
        return f"{self.origin}{self.target}"


def parse_url(raw: object) -> ParsedUrl:
    """Strictly parse *raw* into a :class:`ParsedUrl` or raise :class:`PolicyViolation`.

    The parts are re-assembled by this module and *that* string is what the HTTP
    client receives, so the host that is validated is, by construction, the host
    that is contacted — there is no second parser to disagree about it.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PolicyViolation("The URL is empty.", code="invalid_url")
    text = raw.strip()
    if len(text) > MAX_URL_LENGTH:
        raise PolicyViolation("The URL is too long.", code="invalid_url")
    if _FORBIDDEN_URL_CHARS.search(text):
        raise PolicyViolation(
            "The URL contains whitespace, control characters or a backslash.", code="invalid_url"
        )
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        netloc = parts.netloc
        raw_host = parts.hostname
        port = parts.port
    except ValueError:
        raise PolicyViolation("The URL could not be parsed.", code="invalid_url") from None
    if scheme not in _DEFAULT_PORTS:
        raise PolicyViolation(
            f"Only http and https URLs are supported (got '{scheme or 'none'}').",
            code="invalid_url",
        )
    if "@" in netloc:
        raise PolicyViolation(
            "URLs with embedded credentials are not allowed; use a credential reference.",
            code="invalid_url",
        )
    if not raw_host:
        raise PolicyViolation("The URL has no host.", code="invalid_url")
    if "%" in raw_host:
        raise PolicyViolation("The host contains percent-encoding.", code="invalid_url")

    host = raw_host.lower().rstrip(".")
    ip: IPAddress | None = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    if ip is None:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise PolicyViolation("The host is not a valid name.", code="invalid_url") from None
        if not host or len(host) > 253 or any(not label for label in host.split(".")):
            raise PolicyViolation("The host is not a valid name.", code="invalid_url")
        labels = host.split(".")
        if all(_NUMERIC_LABEL.match(label) for label in labels):
            raise PolicyViolation(
                "The host looks like an IP address in an unusual notation; "
                "write it in standard dotted form.",
                code="invalid_url",
            )
    else:
        host = ip.compressed
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if not 0 < port < 65536:
        raise PolicyViolation("The port is out of range.", code="invalid_url")
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return ParsedUrl(scheme, host, port, path, parts.query, ip)


def parse_origin(raw: str) -> Origin:
    """Parse a configured trusted origin (``http://localhost:5000``)."""
    return parse_url(raw).origin


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]  # the client address embedded in a Teredo address
    if ip in _NAT64 or (ip in _IPV4_COMPATIBLE and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def classify_ip(ip: IPAddress) -> tuple[Kind, str]:
    """Classify one address. Transition addresses use the IPv4 address they embed."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None:
            kind, reason = classify_ip(inner)
            return kind, f"{reason} (embedded in {ip.compressed})"
    if ip in _METADATA_ADDRESSES:
        return Kind.BLOCKED, "a cloud metadata address"
    networks = _ALWAYS_BLOCKED_V4 if ip.version == 4 else _ALWAYS_BLOCKED_V6
    for net in networks:
        if ip in net:
            return Kind.BLOCKED, f"a link-local, unspecified, multicast or reserved address ({net})"
    if ip.is_loopback:
        return Kind.LOOPBACK, "a loopback address"
    for net in _PRIVATE_EXTRA:
        if ip.version == net.version and ip in net:
            return Kind.PRIVATE, f"a private network address ({net})"
    if ip.is_private or not ip.is_global:
        return Kind.PRIVATE, "a private or non-routable address"
    return Kind.PUBLIC, "a public address"


def is_loopback_name(host: str) -> bool:
    """True for ``localhost`` and ``*.localhost`` (RFC 6761: always loopback)."""
    return host == "localhost" or host.endswith(".localhost")


@dataclass(frozen=True, slots=True)
class Destination:
    """The policy's judgement of a URL *from its text alone* (no DNS)."""

    url: ParsedUrl
    kind: Kind  # public for any ordinary hostname: resolution decides at connect time
    trusted: bool
    #: Why this is refused outright (empty when it is not).
    blocked: str = ""
    #: Why this deserves a human's attention beyond the request method itself.
    notes: tuple[str, ...] = ()
    #: True when the destination is a sensitive local service (HIGH risk).
    sensitive: bool = False

    @property
    def local(self) -> bool:
        return self.kind in (Kind.LOOPBACK, Kind.PRIVATE)


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve *host* with the system resolver (all addresses, TCP)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for _family, _type, _proto, _name, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]  # drop an IPv6 zone id
        if address not in seen:
            seen.append(address)
    return seen


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Which destinations HTTP requests may reach.

    ``trusted_origins`` are exact ``scheme://host:port`` matches the user (or an
    environment's own base URL) vouches for. Nothing here is matched by prefix or
    wildcard.
    """

    trusted_origins: frozenset[Origin] = frozenset()
    allow_private_network: bool = False
    #: ``localhost`` and loopback IP literals are reachable (a dev server) — the
    #: gate still asks about untrusted ones. False makes every loopback target refused.
    allow_loopback: bool = True
    resolve_timeout: float = 5.0
    extra_blocked_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_origins(cls, origins: Iterable[str], **kwargs: object) -> NetworkPolicy:
        parsed = set()
        for text in origins:
            try:
                parsed.add(parse_origin(text))
            except PolicyViolation:
                continue  # an unusable entry grants nothing
        return cls(trusted_origins=frozenset(parsed), **kwargs)  # type: ignore[arg-type]

    def with_trusted(self, *origins: Origin) -> NetworkPolicy:
        return NetworkPolicy(
            trusted_origins=self.trusted_origins | frozenset(origins),
            allow_private_network=self.allow_private_network,
            allow_loopback=self.allow_loopback,
            resolve_timeout=self.resolve_timeout,
            extra_blocked_hosts=self.extra_blocked_hosts,
        )

    def is_trusted(self, origin: Origin) -> bool:
        return origin in self.trusted_origins

    # ── Before the request: classification from the URL text ────────────────

    def classify(self, url: ParsedUrl) -> Destination:
        trusted = self.is_trusted(url.origin)
        host = url.host
        if host in _METADATA_HOSTS or host in self.extra_blocked_hosts:
            return Destination(url, Kind.BLOCKED, False, blocked=f"'{host}' is a blocked host")
        if url.ip is not None:
            kind, reason = classify_ip(url.ip)
        elif is_loopback_name(host):
            kind, reason = Kind.LOOPBACK, "localhost"
        else:
            kind, reason = Kind.PUBLIC, "a named host"
        if kind is Kind.BLOCKED:
            return Destination(url, kind, False, blocked=f"{url.host} is {reason}")
        notes: list[str] = []
        sensitive = False
        if kind is Kind.LOOPBACK and not self.allow_loopback and not trusted:
            return Destination(
                url, kind, False, blocked="loopback destinations are disabled by the policy"
            )
        if kind is Kind.PRIVATE and not (trusted or self.allow_private_network):
            return Destination(
                url,
                kind,
                False,
                blocked=f"{url.host} is {reason}; add its origin to the trusted origins to allow it",
            )
        if kind in (Kind.LOOPBACK, Kind.PRIVATE) and not trusted:
            service = SENSITIVE_LOCAL_PORTS.get(url.port)
            if service:
                sensitive = True
                notes.append(f"port {url.port} is usually {service}")
        if url.scheme == "http" and kind is Kind.PUBLIC:
            notes.append("plain HTTP over the internet: the traffic is not encrypted")
        return Destination(url, kind, trusted, notes=tuple(notes), sensitive=sensitive)

    # ── When connecting: the addresses actually about to be used ─────────────

    def check_resolved(self, host: str, port: int, addresses: Iterable[str]) -> list[str]:
        """Validate resolved *addresses* for *host*; return those to connect to.

        Every address must be acceptable — one bad answer in a multi-record
        response rejects the lot, because the client could otherwise be steered
        to it on a retry. The *host text* decides how much a loopback or private
        answer is worth: a literal loopback host or ``localhost`` may reach
        loopback, a trusted origin may reach its own addresses, and a public
        name that points inward is refused.
        """
        origin_trusted = any(o.host == host and o.port == port for o in self.trusted_origins)
        literal: IPAddress | None
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        accepted: list[str] = []
        for text in addresses:
            try:
                ip = ipaddress.ip_address(text)
            except ValueError:
                raise PolicyViolation(
                    "The host resolved to something that is not an address."
                ) from None
            kind, reason = classify_ip(ip)
            if kind is Kind.BLOCKED:
                raise PolicyViolation(
                    f"'{host}' resolves to {reason}, which is never reachable.",
                    code="network_policy_blocked",
                )
            if kind is Kind.LOOPBACK:
                names_loopback = (
                    literal is not None and classify_ip(literal)[0] is Kind.LOOPBACK
                ) or (is_loopback_name(host))
                if not (origin_trusted or (names_loopback and self.allow_loopback)):
                    raise PolicyViolation(
                        f"'{host}' resolves to {reason}. Only localhost, a loopback address or a "
                        "trusted origin may do that (a public name pointing inward is refused).",
                    )
            elif kind is Kind.PRIVATE and not (origin_trusted or self.allow_private_network):
                raise PolicyViolation(
                    f"'{host}' resolves to {reason}. Add its origin to the trusted origins "
                    "to allow private-network targets.",
                )
            accepted.append(text)
        if not accepted:
            raise PolicyViolation(f"'{host}' did not resolve to any address.", code="dns_failed")
        return accepted

    async def resolve(self, host: str, port: int, resolver: Resolver | None = None) -> list[str]:
        """Resolve and validate in one step. Raises :class:`PolicyViolation`."""
        if host in _METADATA_HOSTS or host in self.extra_blocked_hosts:
            raise PolicyViolation(f"'{host}' is a blocked host.")
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                addresses = await asyncio.wait_for(
                    (resolver or system_resolver)(host, port), self.resolve_timeout
                )
            except (asyncio.TimeoutError, TimeoutError):
                raise PolicyViolation(f"Resolving '{host}' timed out.", code="dns_failed") from None
            except OSError:
                raise PolicyViolation(
                    f"'{host}' could not be resolved (DNS lookup failed).", code="dns_failed"
                ) from None
        return self.check_resolved(host, port, addresses)
