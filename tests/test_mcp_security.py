"""Tests for velune.mcp.security's SSRF/trust-gating guard on MCP server URLs.

`validate_mcp_url` is the sole check standing between a user-configured MCP
server URL and an outbound connection velune.mcp actually makes (wired into
all three transports: http, sse, websocket) — a hostile or mistyped URL here
is a classic SSRF vector against cloud-instance metadata endpoints. Found
with zero test coverage during a production-readiness audit pass despite
being exactly the kind of security-critical, easy-to-silently-break code that
most needs it.
"""

from __future__ import annotations

import socket

import pytest

from velune.mcp.security import MCPSecurityError, validate_mcp_url


def _fake_getaddrinfo(ip: str):
    """Build a `socket.getaddrinfo`-shaped result resolving to a single IP."""

    def _fn(host, port, proto=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fn


# ── Baseline: legitimate local-first MCP servers must still work ────────────


def test_loopback_url_is_allowed():
    validate_mcp_url("http://127.0.0.1:8080/sse")


def test_localhost_hostname_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    validate_mcp_url("http://localhost:8080/sse")


def test_lan_address_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.50"))
    validate_mcp_url("http://192.168.1.50:8080/sse")


# ── Structural rejections ────────────────────────────────────────────────────


def test_embedded_credentials_are_rejected():
    with pytest.raises(MCPSecurityError, match="credentials"):
        validate_mcp_url("http://user:pass@example.com/sse")


@pytest.mark.parametrize("scheme", ["ftp", "file", "gopher", "javascript"])
def test_non_http_schemes_are_rejected(scheme):
    with pytest.raises(MCPSecurityError, match="not allowed"):
        validate_mcp_url(f"{scheme}://example.com/sse")


def test_missing_hostname_is_rejected():
    with pytest.raises(MCPSecurityError, match="no hostname"):
        validate_mcp_url("http:///sse")


def test_unparseable_url_is_rejected():
    with pytest.raises(MCPSecurityError, match="Unparseable"):
        validate_mcp_url("http://[::not-an-ipv6")


# ── Cloud metadata / link-local: always blocked, no allowlist bypass ────────


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",  # AWS/Azure/GCP IMDS
        "169.254.170.2",  # AWS ECS task metadata
        "metadata.google.internal",
        "metadata.goog",
        "100.100.100.200",  # Alibaba Cloud metadata
    ],
)
def test_literal_metadata_hosts_are_always_blocked(host):
    with pytest.raises(MCPSecurityError, match="blocked metadata"):
        validate_mcp_url(f"http://{host}/sse")


def test_literal_metadata_host_blocked_even_when_allowlisted():
    """The allowlist can widen what's reachable, never narrow the metadata block."""
    with pytest.raises(MCPSecurityError, match="blocked metadata"):
        validate_mcp_url("http://169.254.169.254/sse", allowed_hosts=["169.254.169.254"])


def test_literal_link_local_ip_is_blocked():
    with pytest.raises(MCPSecurityError, match="link-local"):
        validate_mcp_url("http://169.254.1.1/sse")


def test_dns_rebinding_to_metadata_ip_is_blocked(monkeypatch):
    """A hostname that *resolves* to the metadata IP must be caught too —
    not just a literal metadata IP/hostname in the URL."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    with pytest.raises(MCPSecurityError, match="blocked metadata IP"):
        validate_mcp_url("http://evil.example.com/sse")


def test_dns_rebinding_to_link_local_ip_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.1.1"))
    with pytest.raises(MCPSecurityError, match="link-local"):
        validate_mcp_url("http://evil.example.com/sse")


def test_unresolvable_hostname_is_let_through_to_fail_naturally(monkeypatch):
    def _raise(*_a, **_k):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    validate_mcp_url("http://this-host-does-not-exist.invalid/sse")


# ── Allowlist enforcement ────────────────────────────────────────────────────


def test_host_not_in_allowlist_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.50"))
    with pytest.raises(MCPSecurityError, match="not in the configured allowlist"):
        validate_mcp_url("http://192.168.1.50:8080/sse", allowed_hosts=["trusted.example.com"])


def test_host_in_allowlist_is_permitted(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.50"))
    validate_mcp_url("http://trusted.example.com/sse", allowed_hosts=["trusted.example.com"])


def test_allowlist_matching_is_case_and_trailing_dot_insensitive(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.50"))
    validate_mcp_url("http://Trusted.Example.com./sse", allowed_hosts=["trusted.example.com"])


def test_empty_allowlist_permits_any_non_blocked_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.50"))
    validate_mcp_url("http://anything.example.com/sse", allowed_hosts=[])
