"""The URL validators must not touch the process-global socket default timeout.

``socket.setdefaulttimeout`` is inherited by every socket created afterwards in
the process (including pywinpty's PTY relay in the TUI tests), and it never
bounded ``getaddrinfo`` anyway. Resolution is mocked: no network is used.
"""

from __future__ import annotations

import socket

import pytest

from velune.mcp.security import MCPSecurityError, validate_mcp_url
from velune.tools.web.validator import _is_private_ip, validate_url


@pytest.fixture(autouse=True)
def _restore_default_timeout():
    before = socket.getdefaulttimeout()
    yield
    socket.setdefaulttimeout(before)  # keep a failing test from leaking into others


def _resolver(ip: str, seen: list):
    def _fn(host, port, proto=None, **kw):
        seen.append(socket.getdefaulttimeout())
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fn


@pytest.mark.parametrize("sentinel", [None, 17.5])
def test_mcp_validator_leaves_default_timeout_alone(monkeypatch, sentinel):
    socket.setdefaulttimeout(sentinel)
    seen: list = []
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34", seen))
    validate_mcp_url("https://example.com/sse")
    assert seen == [sentinel]  # resolver ran, with the caller's timeout untouched
    assert socket.getdefaulttimeout() == sentinel


def test_mcp_validator_blocking_path_leaves_default_timeout_alone(monkeypatch):
    socket.setdefaulttimeout(None)
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("169.254.169.254", []))
    with pytest.raises(MCPSecurityError):
        validate_mcp_url("http://evil.example.com/sse")
    assert socket.getdefaulttimeout() is None


def test_mcp_validator_unresolvable_host_leaves_default_timeout_alone(monkeypatch):
    def _raise(*a, **k):
        raise socket.gaierror("nope")

    socket.setdefaulttimeout(None)
    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    validate_mcp_url("http://nope.invalid/sse")
    assert socket.getdefaulttimeout() is None


@pytest.mark.parametrize("sentinel", [None, 17.5])
def test_web_validator_leaves_default_timeout_alone(monkeypatch, sentinel):
    socket.setdefaulttimeout(sentinel)
    seen: list = []
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34", seen))
    assert _is_private_ip("example.com") == (False, "")
    assert validate_url("https://example.com/")[0] is True
    assert seen and all(t == sentinel for t in seen)
    assert socket.getdefaulttimeout() == sentinel


def test_web_validator_still_blocks_rebinding_and_unresolvable(monkeypatch):
    socket.setdefaulttimeout(None)
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("169.254.169.254", []))
    blocked, reason = _is_private_ip("evil.example.com")
    assert blocked and "169.254.169.254" in reason

    def _raise(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    assert _is_private_ip("nope.invalid") == (False, "")
    assert socket.getdefaulttimeout() is None
