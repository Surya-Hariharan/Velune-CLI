"""The HTTP executor against a real local server (VAIR A3).

Methods, bodies, headers, redirects, limits, timeouts, decompression, error
categories and cleanup — with real sockets, so what is tested is what runs.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import zlib

import pytest

from tests.http_server import json_reply, start_server, text_reply
from velune.resources.base import ErrorCode
from velune.resources.http.client import (
    METHODS,
    HttpExecutor,
    HttpFailure,
    HttpLimits,
    OutRequest,
    build_url,
    validate_headers,
)
from velune.resources.http.netpolicy import NetworkPolicy, parse_url


def req(server, path="/", method="GET", **kw) -> OutRequest:
    return OutRequest(method, parse_url(f"{server.base}{path}"), **kw)


def executor(**limits) -> HttpExecutor:
    return HttpExecutor(NetworkPolicy(), HttpLimits(**limits) if limits else HttpLimits())


@pytest.fixture
async def echo():
    server = await start_server(
        lambda s: json_reply(
            json.dumps(
                {
                    "method": s.method,
                    "path": s.path,
                    "query": s.query,
                    "headers": s.headers,
                    "body": s.body.decode("utf-8", "replace"),
                }
            )
        )
    )
    yield server
    await server.stop()


# ── Request execution ───────────────────────────────────────────────────────


@pytest.mark.parametrize("method", [m for m in METHODS if m != "HEAD"])
async def test_every_supported_method_is_sent_as_written(echo, method):
    outcome = await executor().send(req(echo, "/things", method))
    assert outcome.status == 200
    assert json.loads(outcome.body)["method"] == method


async def test_head_returns_headers_and_no_body(echo):
    outcome = await executor().send(req(echo, "/", "HEAD"))
    assert outcome.status == 200 and outcome.body == b""
    assert outcome.header("content-type") == "application/json"


async def test_query_parameters_are_appended_and_encoded(echo):
    request = req(echo, "/s?fixed=1", query=[("name", "a b&c"), ("é", "ü")])
    outcome = await executor().send(request)
    assert json.loads(outcome.body)["query"] == "fixed=1&name=a%20b%26c&%C3%A9=%C3%BC"


async def test_headers_are_sent_and_defaults_are_added(echo):
    outcome = await executor().send(req(echo, headers=[("X-Trace", "abc")]))
    sent = json.loads(outcome.body)["headers"]
    assert sent["x-trace"] == "abc"
    assert sent["x-request-id"] == outcome.request_id
    assert sent["accept-encoding"] == "gzip, deflate"
    assert sent["user-agent"] == "velune-cli"


async def test_a_caller_supplied_request_id_is_kept(echo):
    outcome = await executor().send(req(echo, headers=[("X-Request-Id", "mine-1")]))
    assert json.loads(outcome.body)["headers"]["x-request-id"] == "mine-1"


async def test_a_body_is_sent_with_its_length(echo):
    request = req(
        echo, "/", "POST", body=b'{"a":1}', headers=[("Content-Type", "application/json")]
    )
    sent = json.loads((await executor().send(request)).body)
    assert sent["body"] == '{"a":1}'
    assert sent["headers"]["content-type"] == "application/json"
    assert sent["headers"]["content-length"] == "7"


async def test_the_response_is_structured_not_an_httpx_object(echo):
    outcome = await executor().send(req(echo, "/x"))
    assert (outcome.status, outcome.reason) == (200, "OK")
    assert outcome.url == f"{echo.base}/x"
    assert outcome.duration_ms > 0 and len(outcome.request_id) == 16
    assert isinstance(outcome.body, bytes)


# ── Header validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Upgrade",
        "TE",
        "Proxy-Authorization",
        "X-HTTP-Method-Override",
        "x-method-override",
        "Expect",
    ],
)
def test_headers_that_break_framing_or_change_the_method_are_refused(name):
    with pytest.raises(HttpFailure) as caught:
        validate_headers([(name, "x")])
    assert caught.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Bad Name", "x"),
        ("X-Ok", "line1\r\nInjected: yes"),
        ("X-Ok", "nul\x00"),
        ("", "x"),
        ("X:Y", "v"),
    ],
)
def test_malformed_headers_are_refused(name, value):
    with pytest.raises(HttpFailure):
        validate_headers([(name, value)])


def test_too_many_headers_are_refused():
    with pytest.raises(HttpFailure):
        validate_headers([(f"X-H{i}", "v") for i in range(100)])


def test_build_url_encodes_extra_query_pairs_and_brackets_ipv6():
    assert (
        build_url(parse_url("http://[::1]:8080/p?a=1"), [("k", "v w")])
        == "http://[::1]:8080/p?a=1&k=v%20w"
    )


# ── Limits ──────────────────────────────────────────────────────────────────


async def test_a_declared_oversized_body_is_refused_without_reading_it():
    server = await start_server(lambda s: (200, {"Content-Type": "text/plain"}, b"x" * 5000))
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor(max_response_bytes=1024).send(req(server))
        assert caught.value.code is ErrorCode.RESPONSE_TOO_LARGE
        assert "1024-byte limit" in caught.value.message
    finally:
        await server.stop()


async def test_a_chunked_body_of_unknown_length_stops_at_the_cap():
    chunk = b"x" * 1000
    body = b"".join(b"%x\r\n" % len(chunk) + chunk + b"\r\n" for _ in range(20)) + b"0\r\n\r\n"
    server = await start_server(
        lambda s: (200, {"Content-Type": "text/plain", "Transfer-Encoding": "chunked"}, body)
    )
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor(max_response_bytes=4096).send(req(server))
        assert caught.value.code is ErrorCode.RESPONSE_TOO_LARGE
    finally:
        await server.stop()


async def test_a_gzip_bomb_cannot_exceed_the_decoded_cap():
    bomb = gzip.compress(b"A" * 50_000_000, compresslevel=9)
    assert len(bomb) < 100_000
    server = await start_server(
        lambda s: (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, bomb)
    )
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor(max_response_bytes=64 * 1024).send(req(server))
        assert caught.value.code is ErrorCode.RESPONSE_TOO_LARGE
    finally:
        await server.stop()


async def test_gzip_and_deflate_bodies_are_decoded():
    payload = json.dumps({"ok": True}).encode()
    for encoding, body in (("gzip", gzip.compress(payload)), ("deflate", zlib.compress(payload))):
        server = await start_server(
            lambda s, e=encoding, b=body: (
                200,
                {"Content-Type": "application/json", "Content-Encoding": e},
                b,
            )
        )
        try:
            assert (await executor().send(req(server))).body == payload
        finally:
            await server.stop()


async def test_raw_deflate_without_a_zlib_header_is_decoded():
    payload = b'{"raw": "deflate"}'
    compressor = zlib.compressobj(wbits=-15)
    body = compressor.compress(payload) + compressor.flush()
    server = await start_server(lambda s: (200, {"Content-Encoding": "deflate"}, body))
    try:
        assert (await executor().send(req(server))).body == payload
    finally:
        await server.stop()


async def test_a_corrupt_compressed_body_is_an_invalid_response():
    server = await start_server(lambda s: (200, {"Content-Encoding": "gzip"}, b"not gzip at all"))
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor().send(req(server))
        assert caught.value.code is ErrorCode.INVALID_RESPONSE
    finally:
        await server.stop()


async def test_an_unsupported_content_encoding_is_refused():
    server = await start_server(lambda s: (200, {"Content-Encoding": "br"}, b"\x00"))
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor().send(req(server))
        assert caught.value.code is ErrorCode.INVALID_RESPONSE
    finally:
        await server.stop()


async def test_an_oversized_request_body_is_refused_before_sending(echo):
    with pytest.raises(HttpFailure) as caught:
        await executor(max_request_bytes=1024).send(req(echo, "/", "POST", body=b"x" * 2048))
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert echo.requests == []


# ── Timeouts, deadlines, cancellation ───────────────────────────────────────


async def test_a_slow_response_hits_the_read_timeout():
    async def handler(seen):
        await asyncio.sleep(2)
        return json_reply("{}")

    server = await start_server(handler)
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor(read_timeout=0.2, deadline=5).send(req(server))
        assert caught.value.code is ErrorCode.TIMEOUT
        assert "reading the response" in caught.value.message
    finally:
        await server.stop()


async def test_the_overall_deadline_bounds_the_whole_request():
    async def handler(seen):
        await asyncio.sleep(3)
        return json_reply("{}")

    server = await start_server(handler)
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(HttpFailure) as caught:
            await executor(read_timeout=30, deadline=0.3).send(req(server))
        assert caught.value.code is ErrorCode.TIMEOUT
        assert asyncio.get_running_loop().time() - started < 1.5
    finally:
        await server.stop()


async def test_a_per_request_deadline_overrides_the_default():
    async def handler(seen):
        await asyncio.sleep(3)
        return json_reply("{}")

    server = await start_server(handler)
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor(read_timeout=30, deadline=30).send(req(server, deadline=0.3))
        assert caught.value.code is ErrorCode.TIMEOUT
    finally:
        await server.stop()


async def test_cancellation_propagates_and_leaves_nothing_open():
    started = asyncio.Event()

    async def handler(seen):
        started.set()
        await asyncio.sleep(30)
        return json_reply("{}")

    server = await start_server(handler)
    try:
        task = asyncio.create_task(executor(read_timeout=60, deadline=60).send(req(server)))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await server.stop()


# ── Failure categories ──────────────────────────────────────────────────────


async def test_connection_refused_is_reported_with_advice():
    server = await start_server(lambda s: json_reply("{}"))
    port = server.port
    await server.stop()  # now nothing is listening
    with pytest.raises(HttpFailure) as caught:
        await executor(connect_timeout=10).send(
            OutRequest("GET", parse_url(f"http://127.0.0.1:{port}/"))
        )
    assert caught.value.code is ErrorCode.CONNECTION_FAILED
    assert "refused" in caught.value.message and "listening" in caught.value.hint


async def test_dns_failure_is_its_own_category():
    async def resolver(host, port):
        raise OSError("no such host")

    exec_ = HttpExecutor(NetworkPolicy(), resolver=resolver)
    with pytest.raises(HttpFailure) as caught:
        await exec_.send(OutRequest("GET", parse_url("http://nope.example/")))
    assert caught.value.code is ErrorCode.DNS_FAILED


async def test_a_server_that_speaks_garbage_is_an_invalid_response():
    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"THIS IS NOT HTTP\r\n\r\n")
        await writer.drain()
        writer.close()

    raw = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = raw.sockets[0].getsockname()[1]
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor().send(OutRequest("GET", parse_url(f"http://127.0.0.1:{port}/")))
        assert caught.value.code in (ErrorCode.INVALID_RESPONSE, ErrorCode.CONNECTION_FAILED)
    finally:
        raw.close()
        await raw.wait_closed()


async def test_error_statuses_are_responses_not_failures():
    server = await start_server(lambda s: text_reply("boom", 500))
    try:
        outcome = await executor().send(req(server))
        assert outcome.status == 500 and outcome.body == b"boom"
    finally:
        await server.stop()


# ── Redirects ───────────────────────────────────────────────────────────────


async def test_same_origin_redirects_are_followed_and_recorded():
    def handler(seen):
        if seen.path == "/old":
            return 302, {"Location": "/new"}, b""
        return json_reply(json.dumps({"at": seen.path}))

    server = await start_server(handler)
    try:
        outcome = await executor().send(req(server, "/old"))
        assert json.loads(outcome.body)["at"] == "/new"
        assert outcome.redirects[0]["status"] == 302 and outcome.redirects[0]["followed"] is True
        assert outcome.url.endswith("/new")
    finally:
        await server.stop()


async def test_redirects_can_be_turned_off():
    server = await start_server(lambda s: (302, {"Location": "/elsewhere"}, b""))
    try:
        outcome = await executor().send(req(server, follow_redirects=False))
        assert outcome.status == 302 and outcome.header("location") == "/elsewhere"
    finally:
        await server.stop()


async def test_a_redirect_loop_is_cut_off_at_the_limit():
    server = await start_server(lambda s: (302, {"Location": "/again"}, b""))
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor(max_redirects=3).send(req(server))
        assert caught.value.code is ErrorCode.REDIRECT_REJECTED
        assert len(caught.value.redirects) == 4
    finally:
        await server.stop()


async def test_a_redirect_to_the_cloud_metadata_address_is_rejected():
    server = await start_server(
        lambda s: (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b"")
    )
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor().send(req(server))
        assert caught.value.code is ErrorCode.REDIRECT_REJECTED
        assert "not allowed" in caught.value.message
    finally:
        await server.stop()


async def test_a_redirect_to_a_malformed_location_is_rejected():
    server = await start_server(lambda s: (302, {"Location": "http://u:p@evil.example/"}, b""))
    try:
        with pytest.raises(HttpFailure) as caught:
            await executor().send(req(server))
        assert caught.value.code is ErrorCode.REDIRECT_REJECTED
    finally:
        await server.stop()


async def test_a_post_302_becomes_a_body_less_get():
    seen_methods = []

    def handler(seen):
        seen_methods.append((seen.method, seen.body))
        return (302, {"Location": "/done"}, b"") if seen.path == "/submit" else json_reply("{}")

    server = await start_server(handler)
    try:
        await executor().send(
            req(server, "/submit", "POST", body=b"data", headers=[("Content-Type", "text/plain")])
        )
        assert seen_methods == [("POST", b"data"), ("GET", b"")]
    finally:
        await server.stop()


async def test_a_307_preserves_method_and_body_on_the_same_origin():
    seen_methods = []

    def handler(seen):
        seen_methods.append((seen.method, seen.body))
        return (307, {"Location": "/v2"}, b"") if seen.path == "/v1" else json_reply("{}")

    server = await start_server(handler)
    try:
        await executor().send(
            req(server, "/v1", "PUT", body=b"abc", headers=[("Content-Type", "text/plain")])
        )
        assert seen_methods == [("PUT", b"abc"), ("PUT", b"abc")]
    finally:
        await server.stop()


async def test_credentials_are_dropped_when_a_redirect_changes_origin():
    target = await start_server(
        lambda s: json_reply(json.dumps({"headers": s.headers, "query": s.query}))
    )
    # A second origin = a different port on the same host.
    source = await start_server(lambda s: (302, {"Location": f"{target.base}/landed"}, b""))
    try:
        request = req(
            source,
            "/start",
            headers=[
                ("Authorization", "Bearer SECRET-TOKEN-123"),
                ("Cookie", "sid=abc"),
                ("X-API-Key", "key-456"),
                ("X-Plain", "keep"),
            ],
            injected_headers=frozenset({"x-api-key"}),
        )
        outcome = await executor().send(request)
        landed = json.loads(outcome.body)["headers"]
        assert (
            "authorization" not in landed and "cookie" not in landed and "x-api-key" not in landed
        )
        assert landed["x-plain"] == "keep"  # ordinary headers survive
        assert outcome.redirects[0]["credentials_dropped"] is True
    finally:
        await source.stop()
        await target.stop()


async def test_credentials_are_kept_on_a_same_origin_redirect():
    def handler(seen):
        if seen.path == "/a":
            return 302, {"Location": "/b"}, b""
        return json_reply(json.dumps({"auth": seen.headers.get("authorization")}))

    server = await start_server(handler)
    try:
        outcome = await executor().send(
            req(server, "/a", headers=[("Authorization", "Bearer t0ken-xyz")])
        )
        assert json.loads(outcome.body)["auth"] == "Bearer t0ken-xyz"
    finally:
        await server.stop()


async def test_an_unsafe_method_is_not_resent_to_a_different_origin():
    target = await start_server(lambda s: json_reply("{}"))
    source = await start_server(lambda s: (307, {"Location": f"{target.base}/x"}, b""))
    try:
        outcome = await executor().send(
            req(source, "/", "POST", body=b"payload", headers=[("Content-Type", "text/plain")])
        )
        assert outcome.status == 307  # handed back, not followed
        assert target.requests == []
        assert outcome.redirects[0]["followed"] is False
        assert any("not re-sent" in w for w in outcome.warnings)
    finally:
        await source.stop()
        await target.stop()


async def test_a_public_host_cannot_redirect_to_loopback():
    local = await start_server(lambda s: json_reply('{"internal": true}'))

    async def resolver(host, port):
        return ["93.184.216.34"]

    class Backend:  # the first hop never connects anywhere real
        pass

    try:
        # Simulate the public origin's reply through a transport factory.
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": f"{local.base}/secret"})

        exec_ = HttpExecutor(
            NetworkPolicy(), transport_factory=lambda verify: httpx.MockTransport(handler)
        )
        with pytest.raises(HttpFailure) as caught:
            await exec_.send(OutRequest("GET", parse_url("https://public.example/start")))
        assert caught.value.code is ErrorCode.REDIRECT_REJECTED
        assert "public host to a local" in caught.value.message
        assert local.requests == []
    finally:
        await local.stop()


async def test_an_https_to_http_downgrade_is_refused():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://other.example/"})

    exec_ = HttpExecutor(
        NetworkPolicy(), transport_factory=lambda verify: httpx.MockTransport(handler)
    )
    with pytest.raises(HttpFailure) as caught:
        await exec_.send(OutRequest("GET", parse_url("https://public.example/")))
    assert "plain HTTP" in caught.value.message


async def test_a_blocked_starting_destination_is_refused_before_any_connection():
    with pytest.raises(HttpFailure) as caught:
        await executor().send(
            OutRequest("GET", parse_url("http://169.254.169.254/latest/meta-data"))
        )
    assert caught.value.code is ErrorCode.NETWORK_POLICY
