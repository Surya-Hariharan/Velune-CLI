"""The pinned transport: DNS rebinding, SNI, certificate validation, pooling (VAIR A3).

The headline guarantee is that the address a name is *validated* as is the address
the socket is *connected* to. These tests use a resolver that lies differently on
every call (the rebinding attack), a recording inner backend, and — for TLS — a real
local server with a throw-away certificate.
"""

from __future__ import annotations

import json

import httpcore
import httpx
import pytest

from tests.http_server import json_reply, make_tls_pair, start_server
from velune.resources.base import ErrorCode
from velune.resources.http.client import HttpExecutor, HttpFailure, HttpLimits, OutRequest
from velune.resources.http.netpolicy import NetworkPolicy, PolicyViolation, parse_url
from velune.resources.http.transport import PinnedBackend, PinnedTransport


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Records the host each connection is opened to, then refuses to connect."""

    def __init__(self):
        self.hosts: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        raise httpcore.ConnectError("recorded; not connecting")

    async def connect_unix_socket(
        self, path, timeout=None, socket_options=None
    ):  # pragma: no cover
        raise AssertionError("never reached")

    async def sleep(self, seconds):  # pragma: no cover
        pass


class RebindingResolver:
    """Answers with a public address first and loopback afterwards (classic rebinding)."""

    def __init__(self, *answers: list[str]):
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls += 1
        return self.answers[min(self.calls, len(self.answers)) - 1]


async def test_the_socket_is_opened_to_the_validated_ip_literal_not_the_name():
    inner = RecordingBackend()
    resolver = RebindingResolver(["93.184.216.34"])
    backend = PinnedBackend(NetworkPolicy(), resolver=resolver, inner=inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("api.example.com", 443)
    assert inner.hosts == ["93.184.216.34"]  # connected by address, never by name


async def test_the_name_is_resolved_exactly_once_per_connection_attempt():
    inner = RecordingBackend()
    resolver = RebindingResolver(["93.184.216.34"], ["127.0.0.1"])
    backend = PinnedBackend(NetworkPolicy(), resolver=resolver, inner=inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("api.example.com", 443)
    assert resolver.calls == 1  # there is no second lookup for an attacker to answer differently


async def test_a_name_that_answers_publicly_then_inward_cannot_reach_loopback():
    """Validate-then-reconnect would let the second answer through; pinning cannot."""
    server = await start_server(lambda s: json_reply('{"secret": "internal"}'))
    try:
        resolver = RebindingResolver(["93.184.216.34"], ["127.0.0.1"])  # second answer is the trap
        policy = NetworkPolicy()
        inner = RecordingBackend()
        executor = HttpExecutor(policy, HttpLimits(connect_timeout=1))
        executor.transport_factory = lambda verify: PinnedTransport(
            policy, resolver=resolver, inner=inner
        )
        request = OutRequest("GET", parse_url(f"http://rebind.example:{server.port}/"))
        with pytest.raises(HttpFailure):
            await executor.send(request)
        assert inner.hosts == ["93.184.216.34"]  # the validated public address was the target
        assert server.requests == []  # the local server was never contacted
    finally:
        await server.stop()


async def test_a_name_that_resolves_inward_straight_away_is_refused_before_connecting():
    server = await start_server(lambda s: json_reply("{}"))
    try:

        async def resolver(host, port):
            return ["127.0.0.1"]

        executor = HttpExecutor(NetworkPolicy(), resolver=resolver)
        request = OutRequest("GET", parse_url(f"http://innocent.example:{server.port}/"))
        with pytest.raises(HttpFailure) as caught:
            await executor.send(request)
        assert caught.value.code is ErrorCode.NETWORK_POLICY
        assert server.requests == []
    finally:
        await server.stop()


async def test_a_trusted_origin_can_map_a_name_to_loopback_and_keeps_its_host_header():
    server = await start_server(lambda s: json_reply(json.dumps({"host": s.headers["host"]})))
    try:

        async def resolver(host, port):
            return ["127.0.0.1"]

        origin = f"http://api.dev.test:{server.port}"
        executor = HttpExecutor(NetworkPolicy.from_origins([origin]), resolver=resolver)
        outcome = await executor.send(OutRequest("GET", parse_url(origin + "/x")))
        assert outcome.status == 200
        assert (
            server.requests[0].headers["host"] == f"api.dev.test:{server.port}"
        )  # Host is the name
    finally:
        await server.stop()


async def test_every_address_of_a_multi_record_answer_is_validated():
    async def resolver(host, port):
        return ["93.184.216.34", "169.254.169.254"]

    executor = HttpExecutor(NetworkPolicy(), resolver=resolver)
    with pytest.raises(HttpFailure) as caught:
        await executor.send(OutRequest("GET", parse_url("http://multi.example/")))
    assert caught.value.code is ErrorCode.NETWORK_POLICY


async def test_connection_falls_through_validated_addresses_in_order():
    attempts: list[str] = []

    class Flaky(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            attempts.append(host)
            raise httpcore.ConnectError("down")

        async def connect_unix_socket(
            self, path, timeout=None, socket_options=None
        ):  # pragma: no cover
            raise AssertionError

        async def sleep(self, seconds):  # pragma: no cover
            pass

    async def resolver(host, port):
        return ["93.184.216.34", "93.184.216.35"]

    backend = PinnedBackend(NetworkPolicy(), resolver=resolver, inner=Flaky())
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("api.example.com", 443)
    assert attempts == ["93.184.216.34", "93.184.216.35"]


async def test_unix_sockets_are_never_reachable():
    backend = PinnedBackend(NetworkPolicy())
    with pytest.raises(PolicyViolation):
        await backend.connect_unix_socket("/var/run/docker.sock")


async def test_the_connect_observer_reports_name_address_and_port():
    server = await start_server(lambda s: json_reply("{}"))
    seen: list[tuple[str, str, int]] = []
    try:
        executor = HttpExecutor(
            NetworkPolicy(),
            on_connect=lambda host, address, port: seen.append((host, address, port)),
        )
        await executor.send(OutRequest("GET", parse_url(f"{server.base}/")))
        assert seen == [("127.0.0.1", "127.0.0.1", server.port)]
    finally:
        await server.stop()


# ── TLS: SNI and certificate validation survive pinning ────────────────────


async def test_tls_verifies_the_real_hostname_and_sends_it_as_sni(tmp_path):
    server_ctx, client_ctx, sni_seen = make_tls_pair("api.test.local", tmp_path)
    server = await start_server(lambda s: json_reply('{"secure": true}'), server_ctx)
    try:

        async def resolver(host, port):
            return ["127.0.0.1"]  # the name is pinned to loopback, as a dev setup would

        origin = f"https://api.test.local:{server.port}"
        executor = HttpExecutor(
            NetworkPolicy.from_origins([origin]), resolver=resolver, ssl_context=client_ctx
        )
        outcome = await executor.send(OutRequest("GET", parse_url(origin + "/")))
        assert outcome.status == 200
        assert "api.test.local" in sni_seen  # SNI carried the name, not the pinned address
        assert server.requests[0].headers["host"] == f"api.test.local:{server.port}"
    finally:
        await server.stop()


async def test_a_certificate_for_a_different_name_fails_validation(tmp_path):
    server_ctx, client_ctx, _sni = make_tls_pair("other.test.local", tmp_path)
    server = await start_server(lambda s: json_reply("{}"), server_ctx)
    try:

        async def resolver(host, port):
            return ["127.0.0.1"]

        origin = f"https://api.test.local:{server.port}"
        executor = HttpExecutor(
            NetworkPolicy.from_origins([origin]), resolver=resolver, ssl_context=client_ctx
        )
        with pytest.raises(HttpFailure) as caught:
            await executor.send(OutRequest("GET", parse_url(origin + "/")))
        assert caught.value.code is ErrorCode.TLS_FAILED
        assert server.requests == []  # no request was sent over the unverified connection
    finally:
        await server.stop()


async def test_an_untrusted_certificate_fails_validation_by_default(tmp_path):
    server_ctx, _client_ctx, _sni = make_tls_pair("api.test.local", tmp_path)
    server = await start_server(lambda s: json_reply("{}"), server_ctx)
    try:
        executor = HttpExecutor(NetworkPolicy())  # the system trust store does not know this cert
        with pytest.raises(HttpFailure) as caught:
            await executor.send(OutRequest("GET", parse_url(f"{server.base}/")))
        assert caught.value.code is ErrorCode.TLS_FAILED
    finally:
        await server.stop()


# ── Pooling ─────────────────────────────────────────────────────────────────


async def test_pooled_connections_are_per_executor_send_and_closed_afterwards():
    server = await start_server(lambda s: json_reply("{}"))
    try:
        executor = HttpExecutor(NetworkPolicy())
        for _ in range(3):
            await executor.send(OutRequest("GET", parse_url(f"{server.base}/")))
        assert server.connections == 3  # nothing is shared across separately judged requests
    finally:
        await server.stop()


def test_the_transport_refuses_to_run_without_the_pool_hook(monkeypatch):
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", lambda self, **kw: None)
    with pytest.raises(RuntimeError):
        PinnedTransport(NetworkPolicy())
