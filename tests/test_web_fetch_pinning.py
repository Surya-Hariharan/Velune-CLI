"""WebFetch resolves once and connects to what it validated (the A0 DNS-rebinding finding)."""

from __future__ import annotations

import pytest

from tests.http_server import json_reply, start_server
from velune.resources.http.netpolicy import PolicyViolation
from velune.tools.web import fetch as fetch_module
from velune.tools.web.fetch import WebFetch


async def test_a_name_that_validates_as_public_but_connects_inward_is_refused(monkeypatch):
    """validate_url passes (the name does not resolve for it); the connect-time answer is loopback."""
    server = await start_server(lambda s: json_reply('{"internal": "TOP-SECRET"}'))
    resolved: list[str] = []

    async def hostile_resolver(host, port):
        resolved.append(host)
        return ["127.0.0.1"]

    monkeypatch.setattr("velune.resources.http.netpolicy.system_resolver", hostile_resolver)
    monkeypatch.setattr(fetch_module, "validate_url", lambda url, allow_http=False: (True, None))
    try:
        with pytest.raises(PolicyViolation) as caught:
            await WebFetch().execute(f"https://rebind.example:{server.port}/")
        assert "resolves to a loopback address" in caught.value.message
        assert resolved == ["rebind.example"]  # resolved exactly once, at connect time
        assert server.requests == []
    finally:
        await server.stop()


async def test_a_metadata_answer_at_connect_time_is_refused(monkeypatch):
    async def resolver(host, port):
        return ["93.184.216.34", "169.254.169.254"]

    monkeypatch.setattr("velune.resources.http.netpolicy.system_resolver", resolver)
    monkeypatch.setattr(fetch_module, "validate_url", lambda url, allow_http=False: (True, None))
    with pytest.raises(PolicyViolation):
        await WebFetch().execute("https://innocent.example/")


def test_the_pinned_transport_is_used_unless_a_proxy_performs_the_lookup(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    from velune.resources.http.transport import PinnedTransport

    assert isinstance(fetch_module._pinned_client_options()["transport"], PinnedTransport)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    assert (
        fetch_module._pinned_client_options() == {}
    )  # a proxy resolves names itself: nothing to pin


async def test_loopback_stays_unreachable_through_web_fetch():
    server = await start_server(lambda s: json_reply("{}"))
    try:
        with pytest.raises(ValueError, match="validation failed"):
            await WebFetch().execute(f"http://127.0.0.1:{server.port}/")
        assert server.requests == []
    finally:
        await server.stop()
