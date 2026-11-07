"""PyPI Simple API lookup for `velune update --check` (P2.2). No real network: httpx.MockTransport only.

Success-path tests need a working ``packaging`` (filenames are parsed with
``packaging.utils``); they skip where it is missing.
"""

from __future__ import annotations

import json

import httpx
import pytest

from velune.lifecycle import index
from velune.lifecycle.errors import ExitCode, LifecycleError

pytest.importorskip("packaging.utils")

SIMPLE_TYPE = "application/vnd.pypi.simple.v1+json"


def f(filename, **kw):
    return {"filename": filename, "url": "https://files.example/x", "hashes": {}} | kw


GOOD = {
    "meta": {"api-version": "1.4", "_last-serial": 1},
    "name": "velune-cli",
    "versions": ["0.9.9", "0.9.10"],
    "files": [
        f("velune_cli-0.9.9.tar.gz", **{"requires-python": ">=3.10"}),
        f("velune_cli-0.9.9-py3-none-any.whl", **{"requires-python": ">=3.10"}),
        f("velune_cli-0.9.10.tar.gz", **{"requires-python": ">=3.10"}),
    ],
}
EXPECTED = {
    "0.9.9": [
        {"yanked": False, "requires_python": ">=3.10"},
        {"yanked": False, "requires_python": ">=3.10"},
    ],
    "0.9.10": [{"yanked": False, "requires_python": ">=3.10"}],
}


def serve(handler):
    return httpx.MockTransport(handler)


def fetch_with(response=None, *, handler=None, **kwargs):
    if handler is None:

        def handler(request):  # noqa: ARG001
            return response

    return index.fetch_releases(transport=serve(handler), **kwargs)


def ok(payload, content_type=SIMPLE_TYPE) -> httpx.Response:
    return httpx.Response(
        200, content=json.dumps(payload).encode(), headers={"Content-Type": content_type}
    )


def fails(**kwargs) -> LifecycleError:
    with pytest.raises(LifecycleError) as caught:
        fetch_with(**kwargs)
    return caught.value


def test_success_groups_files_by_version():
    assert fetch_with(ok(GOOD)) == EXPECTED


def test_one_candidate_per_version_however_many_files():
    assert set(fetch_with(ok(GOOD))) == {"0.9.9", "0.9.10"}


def test_asks_the_documented_endpoint_with_a_plain_get():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(GOOD)

    fetch_with(handler=handler)
    (request,) = seen
    assert str(request.url) == "https://pypi.org/simple/velune-cli/"
    assert request.method == "GET"
    assert request.headers["accept"] == SIMPLE_TYPE
    assert request.headers["user-agent"].startswith("velune-cli/")
    assert not {"authorization", "cookie", "x-api-key"} & set(request.headers)


def test_only_metadata_is_requested_never_package_files():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return ok(GOOD)

    fetch_with(handler=handler)
    assert urls == [index.PYPI_SIMPLE_URL]


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
        return httpx.Response(301, headers={"Location": "https://evil.example/simple/"})

    err = fails(handler=handler)
    assert err.details["reason"] == "http_status"
    assert hits == [index.PYPI_SIMPLE_URL]


@pytest.mark.parametrize(
    "body", [b"", b"{not json", b"\xff\xfe\x00", b"<html>captive portal</html>"]
)
def test_invalid_json(body):
    reply = httpx.Response(200, content=body, headers={"Content-Type": SIMPLE_TYPE})
    err = fails(response=reply)
    assert err.code is ExitCode.NETWORK and err.details["reason"] == "invalid_json"


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "application/json", "application/vnd.pypi.simple.v1+html", ""],
)
def test_other_content_types_are_rejected(content_type):
    err = fails(response=ok(GOOD, content_type=content_type))
    assert err.code is ExitCode.NETWORK and err.details["reason"] == "unsupported_content_type"


def test_content_type_parameters_and_case_are_tolerated():
    response = ok(GOOD, content_type=SIMPLE_TYPE.upper() + "; charset=utf-8")
    assert fetch_with(response) == EXPECTED


_META = {"api-version": "1.0"}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        None,
        {},
        {"files": [], "name": "velune-cli"},  # no meta
        {"meta": {}, "files": [], "name": "velune-cli"},
        {"meta": {"api-version": 1.0}, "files": [], "name": "velune-cli"},
        {"meta": {"api-version": "2.0"}, "files": [], "name": "velune-cli"},
        {"meta": {"api-version": ""}, "files": [], "name": "velune-cli"},
        {"meta": "1.0", "files": [], "name": "velune-cli"},
        {"meta": _META, "files": []},  # no name
        {"meta": _META, "name": 5, "files": []},
        {"meta": _META, "name": "velune-cli"},  # no files
        {"meta": _META, "name": "velune-cli", "files": {}},
        {"meta": _META, "name": "velune-cli", "files": "x"},
    ],
)
def test_unexpected_structure(payload):
    err = fails(response=ok(payload))
    assert err.code is ExitCode.NETWORK and err.details["reason"] == "unexpected_structure"


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.4", "1.99"])
def test_any_minor_of_api_major_1_is_accepted(version):
    assert fetch_with(ok(GOOD | {"meta": {"api-version": version}})) == EXPECTED


def test_reply_for_a_different_package_is_rejected():
    err = fails(response=ok(GOOD | {"name": "velune"}))
    assert err.details["reason"] == "unexpected_structure"
    assert "different package" in err.message


def test_package_name_matches_after_normalisation():
    assert fetch_with(ok(GOOD | {"name": "Velune_CLI"})) == EXPECTED


def test_deprecated_releases_field_is_not_used():
    legacy = {"info": {"name": "velune-cli"}, "releases": {"9.9.9": [{"yanked": False}]}}
    err = fails(response=ok(legacy))
    assert err.details["reason"] == "unexpected_structure"
    result = fetch_with(ok(GOOD | {"releases": {"9.9.9": [{"yanked": False}]}}))
    assert "9.9.9" not in result


# ---- filename handling ------------------------------------------------------


def files_reply(*entries):
    return ok(GOOD | {"files": list(entries)})


def test_wheel_and_sdist_names_are_parsed_with_packaging():
    reply = files_reply(
        f("velune_cli-1.2.3rc1-py3-none-any.whl"),
        f("velune_cli-1.2.3rc1.tar.gz"),
        f("velune_cli-1.2.4.post1-1-py3-none-any.whl"),  # has a build tag
    )
    assert set(fetch_with(reply)) == {"1.2.3rc1", "1.2.4.post1"}


def test_versions_are_normalised_so_equal_versions_merge():
    reply = files_reply(f("velune_cli-1.0.0-py3-none-any.whl"), f("velune_cli-1.0.tar.gz"))
    result = fetch_with(reply)
    assert list(result) == ["1.0.0"] and len(result["1.0.0"]) == 2


@pytest.mark.parametrize(
    "filename",
    [
        "velune_cli-1.0.0.egg",
        "velune_cli-1.0.0.exe",
        "velune_cli.tar.gz",
        "velune_cli-banana.tar.gz",
        "velune_cli-1.0.0-py3.whl",  # too few wheel fields
        "velune-cli-1.0.0-py3-none-any.whl",  # hyphen in a wheel project name
        "garbage",
        "",
        "../../etc/passwd.tar.gz",
    ],
)
def test_unsupported_filenames_are_ignored(filename):
    assert fetch_with(files_reply(f(filename))) == {}


def test_files_for_other_projects_are_ignored():
    reply = files_reply(f("other_project-9.9.9.tar.gz"), f("velune_cli-1.0.0.tar.gz"))
    assert list(fetch_with(reply)) == ["1.0.0"]


@pytest.mark.parametrize("entry", [None, 5, "x", [], {}, {"filename": 5}, {"url": "u"}])
def test_malformed_file_entries_are_skipped(entry):
    reply = files_reply(entry, f("velune_cli-1.0.0.tar.gz"))
    assert list(fetch_with(reply)) == ["1.0.0"]


@pytest.mark.parametrize(
    ("yanked", "expected"),
    [
        (False, False),
        (True, True),
        ("broken metadata", True),  # PEP 691: a reason string means yanked
        ("", True),
        (None, True),  # unknown type: do not recommend it
        (1, True),
    ],
)
def test_yanked_flag_is_read_conservatively(yanked, expected):
    (entry,) = fetch_with(files_reply(f("velune_cli-1.0.0.tar.gz", yanked=yanked)))["1.0.0"]
    assert entry["yanked"] is expected


def test_missing_yanked_and_requires_python_mean_not_yanked_and_unrestricted():
    (entry,) = fetch_with(files_reply(f("velune_cli-1.0.0.tar.gz")))["1.0.0"]
    assert entry == {"yanked": False, "requires_python": None}


def test_missing_packaging_is_reported_not_worked_around(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("packaging"):
            raise ImportError("secret detail")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    err = fails(response=ok(GOOD))
    assert err.code is ExitCode.ERROR
    assert err.details["reason"] == "version_library_unavailable"
    assert "secret detail" not in err.message


def test_oversized_reply_is_cut_off():
    reply = httpx.Response(200, content=b"x" * 5000, headers={"Content-Type": SIMPLE_TYPE})
    err = fails(response=reply, max_bytes=1000)
    assert err.details["reason"] == "response_too_large"


# ---- end to end: index reply -> selected release -----------------------------


def _select(reply, *, python=(3, 12, 4), pre=False):
    from velune.lifecycle import versions

    tools = versions.load_version_tools()
    latest = versions.select_latest(
        tools, fetch_with(reply), python=python, include_prereleases=pre
    )
    return None if latest is None else str(latest)


def test_selects_the_highest_compatible_non_yanked_final_release():
    reply = files_reply(
        f("velune_cli-0.9.9.tar.gz", **{"requires-python": ">=3.10"}),
        f("velune_cli-0.9.10.tar.gz", **{"requires-python": ">=3.10"}),
        f("velune_cli-1.0.0.tar.gz", yanked="bad release"),
        f("velune_cli-1.1.0.tar.gz", **{"requires-python": ">=3.13"}),
        f("velune_cli-1.2.0rc1.tar.gz"),
    )
    assert _select(reply) == "0.9.10"
    assert _select(reply, python=(3, 13, 0)) == "1.1.0"
    assert _select(reply, pre=True) == "1.2.0rc1"


def test_a_version_is_usable_if_any_of_its_files_is_usable():
    reply = files_reply(
        f("velune_cli-2.0.0.tar.gz", yanked=True),
        f("velune_cli-2.0.0-py3-none-any.whl", **{"requires-python": ">=3.10"}),
    )
    assert _select(reply) == "2.0.0"


def test_nothing_usable_selects_none():
    reply = files_reply(f("velune_cli-2.0.0.tar.gz", yanked=True), f("velune_cli-3.0.0.egg"))
    assert _select(reply) is None


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
