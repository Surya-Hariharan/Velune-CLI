"""Trust gating for outbound connections to external MCP servers.

An MCP server URL is an *external trust boundary*: Velune connects to it, lists
its tools, and exposes those tools to the model. A hostile or mistyped URL is a
classic SSRF vector — pointing the client at ``http://169.254.169.254/...`` would
let it reach the cloud-instance metadata endpoint and exfiltrate credentials.

Unlike the general web-fetch guard (:mod:`velune.tools.web.validator`), MCP
servers are frequently *local* (``http://127.0.0.1:PORT/sse``), so blanket
private/loopback blocking is wrong here. Instead this guard:

* rejects embedded URL credentials and non-HTTP(S) schemes;
* **always** blocks cloud-metadata and link-local targets (resolved via DNS so
  rebinding tricks can't slip a hostname past the check);
* enforces an optional host **allowlist** — when configured, only those exact
  hosts may be connected to (deny-by-default for everything else).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

#: Cloud-metadata hostnames/IPs that no legitimate MCP server uses.
_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / GCP IMDS
        "169.254.170.2",  # AWS ECS task metadata
        "metadata.google.internal",
        "metadata.goog",
        "100.100.100.200",  # Alibaba Cloud metadata
        "fd00:ec2::254",  # AWS IPv6 IMDS
    }
)


class MCPSecurityError(ValueError):
    """Raised when an MCP server URL fails trust validation."""


def _ip_is_blocked(ip_str: str) -> str | None:
    """Return a reason if *ip_str* is a metadata/link-local target, else None."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    if ip.is_link_local:
        return "link-local address (cloud metadata range)"
    return None


def validate_mcp_url(url: str, allowed_hosts: Iterable[str] | None = None) -> None:
    """Validate an external MCP server *url* before connecting. Raises on failure.

    Loopback and LAN targets are permitted (local-first MCP), but cloud-metadata
    and link-local endpoints are always rejected, and — when *allowed_hosts* is
    non-empty — the host must appear in that allowlist.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise MCPSecurityError(f"Unparseable MCP server URL: {url!r}") from exc

    if parsed.username or parsed.password:
        raise MCPSecurityError("MCP server URLs must not embed credentials.")

    if parsed.scheme not in ("http", "https"):
        raise MCPSecurityError(
            f"MCP server scheme '{parsed.scheme}' not allowed — use http or https."
        )

    hostname = (parsed.hostname or "").lower().strip(".")
    if not hostname:
        raise MCPSecurityError("MCP server URL has no hostname.")

    allow = {h.lower().strip(".") for h in (allowed_hosts or []) if h}
    if allow and hostname not in allow:
        raise MCPSecurityError(
            f"MCP host '{hostname}' is not in the configured allowlist {sorted(allow)}."
        )

    if hostname in _METADATA_HOSTS:
        raise MCPSecurityError(f"MCP host '{hostname}' is a blocked metadata endpoint.")

    # Block the literal hostname if it is itself a link-local IP.
    reason = _ip_is_blocked(hostname)
    if reason:
        raise MCPSecurityError(f"MCP host '{hostname}' rejected: {reason}.")

    # Resolve hostnames and block if ANY resolved address is metadata/link-local,
    # defeating DNS-rebinding (evil.example.com -> 169.254.169.254).
    try:
        socket.setdefaulttimeout(3)
        for *_unused, sockaddr in socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP):
            resolved_ip = sockaddr[0]
            if resolved_ip in _METADATA_HOSTS:
                raise MCPSecurityError(
                    f"MCP host '{hostname}' resolves to blocked metadata IP {resolved_ip}."
                )
            reason = _ip_is_blocked(resolved_ip)
            if reason:
                raise MCPSecurityError(
                    f"MCP host '{hostname}' resolves to {resolved_ip}: {reason}."
                )
    except socket.gaierror:
        # Unresolvable — let the connection attempt fail naturally downstream.
        return
