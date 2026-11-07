"""PyPI metadata lookup for `velune update --check` (P2.2). No real network: httpx.MockTransport only."""

from __future__ import annotations

import json

import httpx
import pytest

from velune.lifecycle import index
from velune.lifecycle.errors import ExitCode, LifecycleError

GOOD = {
    "info": {"name": "velune-cli", "version": "0.9.10"},
    "releases": {
        "0.9.9": [{"yanked": False, "requires_python": ">=3.10"}],
        "0.9.10": [{"yanked": False, "requires_python": ">=3.10"}],
    },
}


def serve(handler):
    return httpx.MockTransport(handler)


def fetch_with(response=None, *, handler=None, **kwargs):
    if handler is None:

        def handler(request):  # noqa: ARG001
            return response

    return index.fetch_releases(transport=serve(handler), **kwargs)


def ok(payload) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode())


def fails(**kwargs) -> LifecycleError:
    with pytest.raises(LifecycleError) as caught:
        fetch_with(**kwargs)
    return caught.value


def test_success_returns_the_release_mapping():
    assert fetch_with(ok(GOOD)) == GOOD["releases"]


def test_asks_the_documented_endpoint_with_a_plain_get():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(GOOD)

    fetch_with(handler=handler)
    (request,) = seen
    assert str(request.url) == "https://pypi.org/pypi/velune-cli/json"
    assert request.method == "GET"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"].startswith("velune-cli/")
    assert not {"authorization", "cookie", "x-api-key"} & set(request.headers)


def test_only_metadata_is_requested_never_package_files():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return ok(GOOD)

    fetch_with(handler=handler)
    assert urls == [index.PYPI_JSON_URL]


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.PoolTimeout("t")],
)
def test_timeouts_are_a_network_error(exc):
    def handler(request):
        raise exc

    err = fails(handler=handler)
    assert err.code is ExitCode.NETWORK
    assert err.details["reason"] == "timeout"
    assert "Nothing was changed" in err.message


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("x"), httpx.ReadError("x"), httpx.ProxyError("x")]
)
def test_connection_failures_are_a_network_error(exc):
    def handler(request):
        raise exc

    err = fails(handler=handler)
    assert err.code is ExitCode.NETWORK and err.details["reason"] == "connection_failed"


def test_exception_text_is_never_shown():
    def handler(request):
        raise httpx.ConnectError("proxy http://user:hunter2@10.0.0.9:3128 C:\\Users\\bob\\x")

    err = fails(handler=handler)
    blob = err.message + (err.recovery or "") + json.dumps(err.details)
    assert "hunter2" not in blob and "bob" not in blob and "10.0.0.9" not in blob
    assert err.details["error_type"] == "ConnectError"


@pytest.mark.parametrize("status", [400, 403, 404, 500, 502, 503])
def test_http_errors(status):
    err = fails(response=httpx.Response(status))
    assert err.code is ExitCode.NETWORK
    assert err.details == {"reason": "http_status", "status": status}
    assert str(status) in err.message


def test_rate_limit_reports_retry_after():
    err = fails(response=httpx.Response(429, headers={"Retry-After": "120"}))
    assert err.code is ExitCode.NETWORK and err.details["reason"] == "rate_limited"
    assert "429" in err.message and "120 seconds" in err.message


@pytest.mark.parametrize("value", ["", "soon", "-5", "9" * 20, "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_rate_limit_ignores_unusable_retry_after(value):
    err = fails(response=httpx.Response(429, headers={"Retry-After": value}))
    assert err.details["reason"] == "rate_limited"
    assert "seconds" not in err.message


def test_redirects_are_not_followed():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(301, headers={"Location": "https://evil.example/json"})

    err = fails(handler=handler)
    assert err.details["reason"] == "http_status"
    assert hits == [index.PYPI_JSON_URL]


@pytest.mark.parametrize(
    "body", [b"", b"{not json", b"\xff\xfe\x00", b"<html>captive portal</html>"]
)
def test_invalid_json(body):
    err = fails(response=httpx.Response(200, content=body))
    assert err.code is ExitCode.NETWORK and err.details["reason"] == "invalid_json"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        None,
        {},
        {"releases": {}},
        {"info": {}, "releases": {}},
        {"info": {"name": 5}, "releases": {}},
        {"info": {"name": "velune-cli"}},
        {"info": {"name": "velune-cli"}, "releases": []},
        {"info": {"name": "velune-cli"}, "releases": "x"},
    ],
)
def test_unexpected_structure(payload):
    err = fails(response=ok(payload))
    assert err.code is ExitCode.NETWORK and err.details["reason"] == "unexpected_structure"


def test_reply_for_a_different_package_is_rejected():
    payload = {"info": {"name": "velune"}, "releases": GOOD["releases"]}
    err = fails(response=ok(payload))
    assert err.details["reason"] == "unexpected_structure"
    assert "different package" in err.message


def test_package_name_matches_after_normalisation():
    payload = {"info": {"name": "Velune_CLI"}, "releases": GOOD["releases"]}
    assert fetch_with(ok(payload)) == GOOD["releases"]


def test_oversized_reply_is_cut_off():
    err = fails(response=httpx.Response(200, content=b"x" * 5000), max_bytes=1000)
    assert err.details["reason"] == "response_too_large"


# ---- index override warnings ----------------------------------------------


def test_no_warning_for_a_clean_environment():
    assert index.index_override_warnings({"PATH": "/bin"}) == []


def test_warns_by_name_and_never_prints_values():
    env = {
        "UV_INDEX_URL": "https://user:token123@corp.example/simple",
        "PIP_INDEX_URL": "x",
        "EMPTY": "",
    }
    (warning,) = index.index_override_warnings(env)
    assert "UV_INDEX_URL" in warning and "PIP_INDEX_URL" in warning
    assert "token123" not in warning and "corp.example" not in warning


def test_empty_values_do_not_warn():
    assert index.index_override_warnings({"UV_INDEX_URL": ""}) == []
