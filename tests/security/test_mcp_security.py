"""SSRF / trust-boundary tests for external MCP server connections.

These exercise :func:`velune.mcp.security.validate_mcp_url` directly — the guard
the MCP client runs before opening an SSE connection — using static blocklisted
targets and a monkeypatched resolver so no real network/DNS is touched.
"""

from __future__ import annotations

import pytest

from velune.mcp.security import MCPSecurityError, validate_mcp_url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # AWS/Azure/GCP IMDS
        "http://169.254.170.2/v2/credentials",  # ECS task metadata
        "https://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/latest/meta-data/",  # Alibaba
    ],
)
def test_metadata_endpoints_are_blocked(url: str) -> None:
    with pytest.raises(MCPSecurityError):
        validate_mcp_url(url)


def test_link_local_ip_is_blocked() -> None:
    with pytest.raises(MCPSecurityError, match="link-local"):
        validate_mcp_url("http://169.254.10.20/sse")


def test_embedded_credentials_rejected() -> None:
    with pytest.raises(MCPSecurityError, match="credentials"):
        validate_mcp_url("https://user:secret@example.com/sse")


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/"])
def test_non_http_schemes_rejected(url: str) -> None:
    with pytest.raises(MCPSecurityError, match="scheme"):
        validate_mcp_url(url)


def test_local_mcp_server_is_allowed() -> None:
    # Local-first: loopback MCP servers are the common case and must work.
    validate_mcp_url("http://127.0.0.1:8080/sse")
    validate_mcp_url("http://localhost:3000/sse")


def test_allowlist_denies_unlisted_host() -> None:
    with pytest.raises(MCPSecurityError, match="allowlist"):
        validate_mcp_url("https://evil.example.com/sse", allowed_hosts=["trusted.example.com"])


def test_allowlist_permits_listed_host() -> None:
    validate_mcp_url("https://trusted.example.com/sse", allowed_hosts=["trusted.example.com"])


def test_dns_rebinding_to_metadata_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that resolves to the metadata IP must be rejected post-resolution."""
    import socket

    def fake_getaddrinfo(host, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(MCPSecurityError, match="metadata|link-local|169.254"):
        validate_mcp_url("https://sneaky.attacker.example/sse")
