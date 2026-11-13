"""The HTTP connector: requests, results, authentication and the permission mapping (VAIR A3)."""

from __future__ import annotations

import json

import pytest

from tests.http_env import decide, http_connector, http_env, verdict
from tests.http_fakes import isolate_keystore
from tests.http_server import json_reply, start_server, text_reply
from velune.core.redaction import redact_secrets
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode
from velune.resources.base import ErrorCode
from velune.resources.http.auth import save_credential

MANUAL, AUTO, PLAN = ExecutionMode.MANUAL, ExecutionMode.AUTO, ExecutionMode.PLAN


@pytest.fixture(autouse=True)
def keystore(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path / "keys")


@pytest.fixture
async def api():
    """A local API that echoes what it received."""

    def handler(seen):
        if seen.path == "/json":
            return json_reply(
                '{"data": {"id": 7, "status": "active"}, "password": "hunter2hunter2", "token": "tok-visible-123"}'
            )
        if seen.path == "/big":
            return json_reply(json.dumps({"rows": ["x" * 100] * 200}))
        if seen.path == "/html":
            return 200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>hello</html>"
        if seen.path == "/binary":
            return 200, {"Content-Type": "image/png"}, bytes(range(256))
        if seen.path == "/badjson":
            return json_reply("{not json")
        if seen.path == "/set-cookie":
            return (
                200,
                {
                    "Content-Type": "text/plain",
                    "Set-Cookie": "session=abc123supersecret; HttpOnly",
                    "X-Env": "staging",
                },
                b"ok",
            )
        if seen.path == "/missing":
            return text_reply("nope", 404)
        if seen.path == "/boom":
            return text_reply("kaboom", 500)
        if seen.path == "/401":
            return text_reply("who?", 401)
        if seen.path == "/403":
            return text_reply("no", 403)
        return json_reply(
            json.dumps(
                {
                    "method": seen.method,
                    "path": seen.path,
                    "query": seen.query,
                    "headers": seen.headers,
                    "body": seen.body.decode("utf-8", "replace"),
                }
            )
        )

    server = await start_server(handler)
    yield server
    await server.stop()


def connector(tmp_path, api, **kw):
    kw.setdefault("trusted", (api.base,))
    return http_connector(tmp_path, **kw)


async def call(conn, **params):
    params.setdefault("method", "GET")
    return await conn.execute("request", params)


# ── Result shape ────────────────────────────────────────────────────────────


async def test_a_response_is_structured_json_not_an_httpx_object(tmp_path, api):
    result = await call(connector(tmp_path, api), url=f"{api.base}/json")
    assert result.ok
    data = result.data
    assert data["status"] == 200 and data["reason"] == "OK" and data["outcome"] == "success"
    assert data["content_type"] == "application/json"
    assert data["json"]["data"] == {"id": 7, "status": "active"}
    assert {"request_id", "duration_ms", "headers", "url", "method", "size_bytes"} <= set(data)
    json.dumps(result.to_dict())  # fully JSON-serializable


@pytest.mark.parametrize(
    ("path", "outcome"),
    [
        ("/missing", "client_error"),
        ("/boom", "server_error"),
        ("/401", "authentication_failed"),
        ("/403", "authorization_denied"),
        ("/json", "success"),
    ],
)
async def test_status_classes_are_named_for_the_model(tmp_path, api, path, outcome):
    result = await call(connector(tmp_path, api), url=f"{api.base}{path}")
    assert result.ok and result.data["outcome"] == outcome


async def test_text_html_and_binary_bodies(tmp_path, api):
    conn = connector(tmp_path, api)
    html = await call(conn, url=f"{api.base}/html")
    assert html.data["text"] == "<html>hello</html>" and "json" not in html.data
    binary = await call(conn, url=f"{api.base}/binary")
    assert (
        binary.data["binary"] is True
        and binary.data["size_bytes"] == 256
        and binary.data["body"] is None
    )


async def test_invalid_json_is_reported_not_swallowed(tmp_path, api):
    result = await call(connector(tmp_path, api), url=f"{api.base}/badjson")
    assert result.ok and result.data["text"] == "{not json"
    assert "not valid JSON" in result.data["json_error"]


async def test_a_large_json_body_is_previewed_and_marked_truncated(tmp_path, api):
    result = await call(connector(tmp_path, api), url=f"{api.base}/big")
    assert result.data["body_truncated"] is True and len(result.data["json_preview"]) <= 6000
    assert "json" not in result.data


async def test_select_extracts_paths_from_a_large_response(tmp_path, api):
    result = await call(
        connector(tmp_path, api), url=f"{api.base}/json", select=["data.id", "data.ghost"]
    )
    assert result.data["selected"] == {"data.id": 7, "data.ghost": "<missing>"}


async def test_every_method_is_sent(tmp_path, api):
    conn = connector(tmp_path, api)
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        result = await call(conn, method=method, url=f"{api.base}/echo")
        assert result.data["json"]["method"] == method
    head = await call(conn, method="HEAD", url=f"{api.base}/echo")
    assert head.data["status"] == 200 and head.data["size_bytes"] == 0


async def test_response_headers_are_filtered_and_secrets_masked(tmp_path, api):
    result = await call(connector(tmp_path, api), url=f"{api.base}/set-cookie")
    assert result.data["headers"]["set-cookie"] == "***REDACTED***"
    assert result.data["headers"]["x-env"] == "staging"
    assert "supersecret" not in json.dumps(result.to_dict())


async def test_sensitive_response_fields_are_masked_and_configured_ones_too(tmp_path, api):
    shown = (await call(connector(tmp_path, api), url=f"{api.base}/json")).data["json"]
    assert shown["password"] == "***REDACTED***" and shown["token"] == "***REDACTED***"
    custom = await call(
        connector(tmp_path, api, redact_response_fields=["status"]), url=f"{api.base}/json"
    )
    assert custom.data["json"]["data"]["status"] == "***REDACTED***"
    assert custom.data["json"]["data"]["id"] == 7


# ── Request configuration ───────────────────────────────────────────────────


async def test_query_headers_json_form_and_raw_bodies(tmp_path, api):
    conn = connector(tmp_path, api)
    sent = (
        await call(
            conn,
            method="POST",
            url=f"{api.base}/echo?a=1",
            query=["b=two words"],
            headers=["X-Trace: t-1", "Accept: application/json"],
            json='{"k": [1, 2]}',
        )
    ).data["json"]
    assert sent["query"] == "a=1&b=two%20words"
    assert (
        sent["headers"]["x-trace"] == "t-1"
        and sent["headers"]["content-type"] == "application/json"
    )
    assert json.loads(sent["body"]) == {"k": [1, 2]}
    form = (await call(conn, method="POST", url=f"{api.base}/echo", form=["a=1", "b=x y"])).data[
        "json"
    ]
    assert (
        form["body"] == "a=1&b=x+y" and "x-www-form-urlencoded" in form["headers"]["content-type"]
    )
    raw = (
        await call(
            conn, method="POST", url=f"{api.base}/echo", body="<a/>", content_type="application/xml"
        )
    ).data["json"]
    assert raw["body"] == "<a/>" and raw["headers"]["content-type"] == "application/xml"


async def test_multipart_fields_and_workspace_files(tmp_path, api):
    (tmp_path / "notes.txt").write_text("file contents here", encoding="utf-8")
    conn = connector(tmp_path, api)
    sent = (
        await call(
            conn,
            method="POST",
            url=f"{api.base}/echo",
            multipart=["title=Report", "doc=@notes.txt"],
        )
    ).data["json"]
    assert sent["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    assert (
        "Report" in sent["body"]
        and "file contents here" in sent["body"]
        and 'filename="notes.txt"' in sent["body"]
    )


@pytest.mark.parametrize("path", ["../outside.txt", "missing.txt", ".env", "sub/../../escape.txt"])
async def test_uploads_are_confined_to_existing_non_secret_workspace_files(tmp_path, api, path):
    (tmp_path / ".env").write_text("API_KEY=abc", encoding="utf-8")
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    result = await call(
        connector(tmp_path, api), method="POST", url=f"{api.base}/echo", multipart=[f"f=@{path}"]
    )
    assert not result.ok and result.code in (ErrorCode.VALIDATION_FAILED, ErrorCode.NOT_FOUND)
    assert api.requests == []  # nothing was sent


@pytest.mark.parametrize(
    "params",
    [
        {"method": "POST", "json": "{bad"},
        {"method": "POST", "json": "{}", "form": ["a=1"]},  # two body kinds
        {"method": "POST", "body": "x"},  # raw body without a content type
        {"method": "GET", "query": ["no-equals-sign"]},
        {"method": "GET", "headers": ["no colon here"]},
        {"method": "GET", "headers": ["Host: evil.example"]},
        {"method": "GET", "headers": ["X-HTTP-Method-Override: DELETE"]},
        {"method": "GET", "expect": ["status === 200"]},
        {"method": "FETCH"},
        {"method": "GET", "auth": "Not A Ref"},
    ],
)
async def test_invalid_requests_fail_validation_without_sending(tmp_path, api, params):
    result = await call(connector(tmp_path, api), url=f"{api.base}/echo", **params)
    assert not result.ok and result.code in (ErrorCode.VALIDATION_FAILED,)
    assert api.requests == []


@pytest.mark.parametrize(
    "url", ["", "ftp://x/", "http://user:pw@host/", "not a url", "http://127.1/"]
)
async def test_invalid_urls_are_their_own_error_category(tmp_path, api, url):
    result = await call(connector(tmp_path, api), url=url)
    assert not result.ok and result.code is ErrorCode.INVALID_URL


async def test_the_timeout_parameter_bounds_the_request(tmp_path):
    import asyncio

    async def slow(seen):
        await asyncio.sleep(3)
        return json_reply("{}")

    server = await start_server(slow)
    try:
        conn = http_connector(tmp_path, trusted=(server.base,))
        result = await call(conn, url=f"{server.base}/", timeout=1)
        assert not result.ok and result.code is ErrorCode.TIMEOUT
    finally:
        await server.stop()


async def test_failures_are_normalized_results_not_exceptions(tmp_path):
    server = await start_server(lambda s: json_reply("{}"))
    port = server.port
    await server.stop()
    conn = http_connector(tmp_path, trusted=(f"http://127.0.0.1:{port}",), connect_timeout=10)
    result = await call(conn, url=f"http://127.0.0.1:{port}/")
    assert (
        not result.ok and result.code is ErrorCode.CONNECTION_FAILED and "refused" in result.error
    )
    assert result.hint


async def test_blocked_destinations_are_a_policy_failure_with_no_connection(tmp_path):
    result = await call(http_connector(tmp_path), url="http://169.254.169.254/latest/meta-data/")
    assert not result.ok and result.code is ErrorCode.NETWORK_POLICY


# ── Assertions in a request ─────────────────────────────────────────────────


async def test_assertions_pass_and_fail_deterministically(tmp_path, api):
    conn = connector(tmp_path, api)
    good = await call(
        conn,
        url=f"{api.base}/json",
        expect=[
            "status == 200",
            "header content-type contains application/json",
            "json data.id exists",
            "json data.status == active",
            "duration < 5000",
        ],
    )
    assert good.data["assertions"]["passed"] and good.data["outcome"] == "success"
    bad = await call(
        conn, url=f"{api.base}/json", expect=["status == 201", "json data.status == retired"]
    )
    assert bad.ok and bad.data["outcome"] == "assertion_failed"
    assert bad.data["assertions"]["failed_count"] == 2
    assert bad.data["assertions"]["results"][1]["actual"] == "active"


# ── Authentication ──────────────────────────────────────────────────────────


async def test_a_named_credential_is_sent_to_its_own_origin_and_never_echoed(tmp_path, api):
    save_credential(
        "local-token", {"type": "bearer", "origin": api.base, "token": "tok-local-secret-77"}
    )
    result = await call(connector(tmp_path, api), url=f"{api.base}/echo", auth="local-token")
    assert api.requests[-1].headers["authorization"] == "Bearer tok-local-secret-77"
    assert "tok-local-secret-77" not in json.dumps(
        result.to_dict()
    )  # the echo of the header is masked
    assert result.data["json"]["headers"]["authorization"] == "***REDACTED***"


async def test_a_credential_for_another_origin_is_refused_when_named(tmp_path, api):
    save_credential(
        "other-api",
        {"type": "bearer", "origin": "https://other.example.com", "token": "tok-other-xyz1"},
    )
    result = await call(connector(tmp_path, api), url=f"{api.base}/echo", auth="other-api")
    assert not result.ok and result.code is ErrorCode.AUTHENTICATION_FAILED
    assert (
        "is bound to https://other.example.com" in result.error
        and "tok-other-xyz1" not in result.error
    )
    assert api.requests == []  # refused before anything was sent


async def test_an_environment_credential_is_skipped_with_a_warning_off_its_origin(tmp_path, api):
    other = await start_server(
        lambda s: json_reply(json.dumps({"auth": s.headers.get("authorization")}))
    )
    try:
        save_credential(
            "env-token", {"type": "bearer", "origin": api.base, "token": "tok-env-secret-1"}
        )
        conn = http_connector(
            tmp_path,
            trusted=(api.base, other.base),
            environments={"dev": {"base_url": api.base, "auth": "env-token"}},
        )
        result = await call(conn, url=f"{other.base}/", environment="dev")
        assert result.ok and result.data["json"]["auth"] is None  # not sent to the unrelated origin
        assert other.requests[0].headers.get("authorization") is None
        # (the environment's default credential never applied: origin differs)
    finally:
        await other.stop()


async def test_the_environment_credential_is_used_by_default_on_its_origin(tmp_path, api):
    save_credential(
        "env-token", {"type": "bearer", "origin": api.base, "token": "tok-env-secret-2"}
    )
    conn = http_connector(
        tmp_path, environments={"dev": {"base_url": api.base, "auth": "env-token"}}
    )
    await call(conn, url="/echo")
    assert api.requests[-1].headers["authorization"] == "Bearer tok-env-secret-2"
    await call(conn, url="/echo", auth="none")  # explicit opt-out
    assert "authorization" not in api.requests[-1].headers


async def test_basic_and_api_key_credentials(tmp_path, api):
    save_credential(
        "basic-c",
        {"type": "basic", "origin": api.base, "username": "ada", "password": "pw-secret-12"},
    )
    save_credential(
        "key-h",
        {
            "type": "api_key_header",
            "origin": api.base,
            "name": "X-Api-Token",
            "value": "hdr-key-12345",
        },
    )
    save_credential(
        "key-q",
        {"type": "api_key_query", "origin": api.base, "name": "api_key", "value": "qry-key-12345"},
    )
    conn = connector(tmp_path, api)
    await call(conn, url=f"{api.base}/echo", auth="basic-c")
    assert api.requests[-1].headers["authorization"].startswith("Basic ")
    await call(conn, url=f"{api.base}/echo", auth="key-h")
    assert api.requests[-1].headers["x-api-token"] == "hdr-key-12345"
    result = await call(conn, url=f"{api.base}/echo?x=1", auth="key-q")
    assert "api_key=qry-key-12345" in api.requests[-1].target
    assert any("query string" in w for w in result.warnings)  # the explicit warning
    assert "qry-key-12345" not in json.dumps(result.to_dict())  # redacted wherever echoed
    assert "api_key=%2A%2A%2AREDACTED" in result.data["url"] or "api_key" not in result.data["url"]


async def test_a_missing_credential_fails_with_instructions_not_a_silent_unauthenticated_call(
    tmp_path, api
):
    result = await call(connector(tmp_path, api), url=f"{api.base}/echo", auth="never-saved")
    assert not result.ok and result.code is ErrorCode.AUTHENTICATION_FAILED
    assert "/resource configure http never-saved" in result.error and api.requests == []


async def test_oauth_client_credentials_fetch_a_token_then_send_it(tmp_path):
    def handler(seen):
        if seen.path == "/oauth/token":
            assert seen.method == "POST" and b"grant_type=client_credentials" in seen.body
            return json_reply('{"access_token": "oauth-access-tok-9", "expires_in": 3600}')
        return json_reply(json.dumps({"auth": seen.headers.get("authorization")}))

    server = await start_server(handler)
    try:
        save_credential(
            "oauth-c",
            {
                "type": "oauth2_client_credentials",
                "origin": server.base,
                "token_url": f"{server.base}/oauth/token",
                "client_id": "cid",
                "client_secret": "csecret-12345",
            },
        )
        conn = http_connector(tmp_path, trusted=(server.base,))
        await call(conn, url=f"{server.base}/data", auth="oauth-c")
        result = await call(conn, url=f"{server.base}/data", auth="oauth-c")
        assert server.requests[-1].headers["authorization"] == "Bearer oauth-access-tok-9"
        assert sum(r.path == "/oauth/token" for r in server.requests) == 1  # cached
        assert "oauth-access-tok-9" not in json.dumps(result.to_dict())
    finally:
        await server.stop()


# ── Environments and variables in requests ──────────────────────────────────


async def test_variables_environments_and_relative_urls(tmp_path, api):
    conn = http_connector(
        tmp_path,
        environments={
            "dev": {"base_url": api.base, "variables": {"v": "v1"}, "headers": {"X-Env": "dev"}},
            "staging": {"base_url": "https://staging.example.com", "variables": {"v": "v2"}},
        },
    )
    result = await call(conn, url="{{base_url}}/{{v}}/users", environment="dev")
    assert result.data["json"]["path"] == "/v1/users" and result.data["environment"] == "dev"
    assert api.requests[-1].headers["x-env"] == "dev"
    relative = await call(conn, url="/ping", environment="dev")
    assert relative.data["json"]["path"] == "/ping"


async def test_environment_headers_are_only_sent_to_that_environments_origin(tmp_path, api):
    other = await start_server(lambda s: json_reply(json.dumps({"x_env": s.headers.get("x-env")})))
    try:
        conn = http_connector(
            tmp_path,
            trusted=(other.base,),
            environments={"dev": {"base_url": api.base, "headers": {"X-Env": "dev"}}},
        )
        result = await call(conn, url=f"{other.base}/", environment="dev")
        assert result.data["json"]["x_env"] is None
    finally:
        await other.stop()


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"url": "{{base_url}}/x", "environment": "nope"}, "Unknown environment"),
        ({"url": "{{base_url}}/x"}, "Several environments exist"),
        ({"url": "/relative", "environment": None}, "Several environments exist"),
        ({"url": "{{missing}}/x", "environment": "dev"}, "unknown variable"),
    ],
)
async def test_environment_and_variable_errors_are_clear(tmp_path, params, fragment):
    conn = http_connector(
        tmp_path,
        environments={
            "dev": {"base_url": "http://localhost:5000"},
            "staging": {"base_url": "https://s.example.com"},
        },
    )
    params = {k: v for k, v in params.items() if v is not None}
    result = await call(conn, **params)
    assert not result.ok and fragment in result.error


async def test_a_single_environment_is_selected_automatically(tmp_path, api):
    conn = http_connector(tmp_path, environments={"only": {"base_url": api.base}})
    assert (await call(conn, url="/auto")).data["json"]["path"] == "/auto"


async def test_a_relative_url_without_any_environment_is_an_error(tmp_path):
    result = await call(http_connector(tmp_path), url="/users")
    assert not result.ok and "needs an environment" in result.error


async def test_secret_variables_resolve_at_send_time_and_stay_out_of_results(tmp_path, api):
    save_credential("admin-secret", {"type": "secret", "value": "admin-secret-value-1"})
    conn = http_connector(
        tmp_path,
        environments={"dev": {"base_url": api.base, "secret_variables": {"admin": "admin-secret"}}},
    )
    result = await call(conn, url="/echo", headers=["X-Admin: {{admin}}"], environment="dev")
    assert api.requests[-1].headers["x-admin"] == "admin-secret-value-1"
    assert "admin-secret-value-1" not in json.dumps(result.to_dict())


async def test_a_secret_variable_is_not_sent_off_the_environments_origin(tmp_path, api):
    other = await start_server(lambda s: json_reply("{}"))
    try:
        save_credential("admin-secret", {"type": "secret", "value": "admin-secret-value-2"})
        conn = http_connector(
            tmp_path,
            trusted=(other.base,),
            environments={
                "dev": {"base_url": api.base, "secret_variables": {"admin": "admin-secret"}}
            },
        )
        result = await call(
            conn, url=f"{other.base}/", headers=["X-Admin: {{admin}}"], environment="dev"
        )
        assert not result.ok and "may only be sent to its own environment" in result.error
        assert other.requests == []
    finally:
        await other.stop()


async def test_an_invalid_environment_is_reported_not_fatal(tmp_path, api):
    conn = http_connector(
        tmp_path, environments={"good": {"base_url": api.base}, "bad": {"base_url": "ftp://nope"}}
    )
    listing = await conn.execute("environments", {})
    assert [e["name"] for e in listing.data["environments"]] == ["good"]
    assert "bad" in listing.data["problems"]
    result = await call(conn, url="/x", environment="bad")
    assert not result.ok and "base_url" in result.error


async def test_the_environments_listing_has_names_not_secret_values(tmp_path, api):
    save_credential("tok", {"type": "secret", "value": "never-show-this-1"})
    conn = http_connector(
        tmp_path,
        environments={
            "dev": {
                "base_url": api.base,
                "variables": {"v": "1"},
                "secret_variables": {"t": "tok"},
                "auth": "tok",
            }
        },
    )
    shown = json.dumps((await conn.execute("environments", {})).to_dict())
    assert "never-show-this-1" not in shown and '"t"' in shown
    one = await conn.execute("environments", {"name": "dev"})
    assert one.data["environment"]["secret_variables"] == ["t"]
    assert not (await conn.execute("environments", {"name": "ghost"})).ok


# ── Permission mapping ──────────────────────────────────────────────────────

LOOP = "http://127.0.0.1:5000/things"
PUBLIC = "https://api.example.com/things"


@pytest.mark.parametrize(
    ("method", "mode", "expected"),
    [
        ("GET", MANUAL, "allow"),
        ("HEAD", MANUAL, "allow"),
        ("OPTIONS", MANUAL, "allow"),
        ("POST", MANUAL, "ask"),
        ("PUT", MANUAL, "ask"),
        ("PATCH", MANUAL, "ask"),
        ("DELETE", MANUAL, "ask"),
        ("GET", PLAN, "allow"),
        ("HEAD", PLAN, "allow"),
        ("POST", PLAN, "deny"),
        ("PUT", PLAN, "deny"),
        ("PATCH", PLAN, "deny"),
        ("DELETE", PLAN, "deny"),
        ("GET", AUTO, "allow"),
        ("POST", AUTO, "allow"),
        ("PUT", AUTO, "allow"),
        ("PATCH", AUTO, "allow"),
        ("DELETE", AUTO, "ask"),  # a delete is confirmed even in auto mode
    ],
)
def test_the_method_and_mode_decide_a_trusted_local_request(tmp_path, method, mode, expected):
    conn = http_connector(tmp_path, trusted=("http://127.0.0.1:5000",))
    assert verdict(conn, "request", {"method": method, "url": LOOP}, mode) == expected


@pytest.mark.parametrize(
    ("method", "mode", "expected"),
    [
        ("GET", MANUAL, "ask"),  # untrusted destination: a network action of its own
        ("GET", PLAN, "ask"),
        ("GET", AUTO, "allow"),
        ("POST", MANUAL, "ask"),
        ("POST", PLAN, "deny"),
        ("POST", AUTO, "ask"),  # a write to an untrusted remote service is never automatic
        ("DELETE", AUTO, "ask"),
    ],
)
def test_an_untrusted_remote_destination_adds_a_network_confirmation(
    tmp_path, method, mode, expected
):
    assert (
        verdict(http_connector(tmp_path), "request", {"method": method, "url": PUBLIC}, mode)
        == expected
    )


def test_a_trusted_remote_origin_needs_no_extra_network_confirmation(tmp_path):
    conn = http_connector(tmp_path, trusted=("https://api.example.com",))
    assert verdict(conn, "request", {"method": "GET", "url": PUBLIC}) == "allow"
    assert verdict(conn, "request", {"method": "POST", "url": PUBLIC}, AUTO) == "allow"


def test_an_untrusted_loopback_origin_is_confirmed_in_manual_mode_but_not_blocked(tmp_path):
    conn = http_connector(tmp_path)
    assert verdict(conn, "request", {"method": "GET", "url": LOOP}) == "ask"
    assert verdict(conn, "request", {"method": "GET", "url": LOOP}, AUTO) == "allow"


@pytest.mark.parametrize("port", [2375, 5432, 6379])
def test_sensitive_local_services_are_confirmed_even_in_auto_mode(tmp_path, port):
    conn = http_connector(tmp_path)
    assert (
        verdict(conn, "request", {"method": "GET", "url": f"http://127.0.0.1:{port}/"}, AUTO)
        == "ask"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://100.100.100.200/",
        "http://[fd00:ec2::254]/",
        "http://0.0.0.0/",
    ],
)
@pytest.mark.parametrize("mode", [MANUAL, PLAN, AUTO])
def test_metadata_and_link_local_destinations_are_denied_in_every_mode(tmp_path, url, mode):
    conn = http_connector(tmp_path, trusted=(url,))
    assert verdict(conn, "request", {"method": "GET", "url": url}, mode) == "deny"


def test_private_network_destinations_are_denied_unless_trusted(tmp_path):
    url = "http://192.168.1.50:3000/x"
    assert verdict(http_connector(tmp_path), "request", {"method": "GET", "url": url}) == "deny"
    assert (
        verdict(
            http_connector(tmp_path, trusted=("http://192.168.1.50:3000",)),
            "request",
            {"method": "GET", "url": url},
        )
        == "allow"
    )


def test_get_requests_that_look_state_changing_are_treated_as_writes(tmp_path):
    conn = http_connector(tmp_path, trusted=("http://127.0.0.1:5000",))
    for path in ("/users/1/delete", "/logout", "/admin/reset-cache", "/api/v1/purge"):
        assert (
            verdict(conn, "request", {"method": "GET", "url": f"http://127.0.0.1:5000{path}"})
            == "ask"
        )
        assert (
            verdict(conn, "request", {"method": "GET", "url": f"http://127.0.0.1:5000{path}"}, PLAN)
            == "deny"
        )
    assert (
        verdict(conn, "request", {"method": "GET", "url": "http://127.0.0.1:5000/users/1"})
        == "allow"
    )


def test_a_production_environment_makes_every_change_high_risk_in_every_mode(tmp_path):
    conn = http_connector(
        tmp_path,
        environments={
            "prod": {"base_url": "https://api.example.com", "production": True},
            "dev": {"base_url": "http://localhost:5000"},
        },
    )
    for mode in (MANUAL, AUTO):
        assert (
            verdict(conn, "request", {"method": "POST", "url": "/x", "environment": "prod"}, mode)
            == "ask"
        )
    assert (
        verdict(conn, "request", {"method": "POST", "url": "/x", "environment": "dev"}, AUTO)
        == "allow"
    )
    assert (
        verdict(conn, "request", {"method": "GET", "url": "/x", "environment": "prod"}) == "allow"
    )
    _decision, actions = decide(
        conn, "request", {"method": "POST", "url": "/x", "environment": "prod"}, AUTO
    )
    assert any("production" in a.reason for a in actions)


def test_disabled_tls_verification_in_an_environment_is_high_risk(tmp_path):
    conn = http_connector(
        tmp_path, environments={"dev": {"base_url": "https://localhost:8443", "verify_tls": False}}
    )
    assert (
        verdict(conn, "request", {"method": "POST", "url": "/x", "environment": "dev"}, AUTO)
        == "ask"
    )


def test_uploading_a_workspace_file_to_an_untrusted_destination_is_confirmed_in_auto(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    conn = http_connector(tmp_path, trusted=("http://127.0.0.1:5000",))
    local = {"method": "POST", "url": LOOP, "multipart": ["f=@a.txt"]}
    assert verdict(conn, "request", local, AUTO) == "allow"
    remote = {"method": "POST", "url": PUBLIC, "multipart": ["f=@a.txt"]}
    assert verdict(http_connector(tmp_path), "request", remote, AUTO) == "ask"


def test_the_decision_uses_integration_scopes_not_method_names_alone(tmp_path):
    conn = http_connector(tmp_path, trusted=("http://127.0.0.1:5000",))
    scopes = {
        m: decide(conn, "request", {"method": m, "url": LOOP})[1][0].scope
        for m in ("GET", "POST", "DELETE")
    }
    assert scopes == {
        "GET": "http.request.read",
        "POST": "http.request.write",
        "DELETE": "http.request.delete",
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"method": "POST", "url": "{{nope}}/x"}, "ask"),  # classified by its method; fails on use
        (
            {"method": "POST", "url": ""},
            "allow",
        ),  # no target at all: rejected before anything is sent
        ({"method": "POST"}, "allow"),
    ],
)
async def test_an_unresolvable_request_sends_nothing_whatever_it_is_judged(
    tmp_path, api, params, expected
):
    conn = connector(tmp_path, api)
    assert verdict(conn, "request", params) == expected
    result = await conn.execute("request", params)
    assert not result.ok and api.requests == []


def test_diagnose_and_openapi_calls_follow_the_same_rules(tmp_path):
    conn = http_connector(tmp_path)
    assert verdict(conn, "diagnose", {"url": PUBLIC}) == "ask"
    assert verdict(conn, "diagnose", {"url": "http://169.254.169.254/"}) == "deny"


# ── Through the production pipeline (manager, bridge, gate) ─────────────────


async def test_requests_run_through_the_gate_and_are_audited(tmp_path, api):
    conn = connector(tmp_path, api)
    env, asked = http_env(tmp_path, conn, MANUAL)
    get = await env.manager.execute("http", "request", {"method": "GET", "url": f"{api.base}/json"})
    assert get.ok and asked == []  # a trusted GET needs no prompt
    post = await env.manager.execute(
        "http", "request", {"method": "POST", "url": f"{api.base}/echo", "json": "{}"}
    )
    assert post.ok and len(asked) == 1
    assert [r["decision"] for r in env.records] == ["allow", "ask"]
    scopes = [a["scope"] for r in env.records for a in r["actions"]]
    assert scopes == ["http.request.read", "http.request.write"]


async def test_a_denied_request_sends_nothing(tmp_path, api):
    conn = connector(tmp_path, api)
    env, asked = http_env(tmp_path, conn, MANUAL, answer=Approval.DENY)
    result = await env.manager.execute(
        "http", "request", {"method": "DELETE", "url": f"{api.base}/echo"}
    )
    assert result.denied and not result.ok and api.requests == []


async def test_plan_mode_never_sends_a_state_changing_request(tmp_path, api):
    conn = connector(tmp_path, api)
    env, asked = http_env(tmp_path, conn, PLAN)
    result = await env.manager.execute(
        "http", "request", {"method": "POST", "url": f"{api.base}/echo", "json": "{}"}
    )
    assert result.denied and "plan mode" in result.error and api.requests == [] and asked == []
    assert (
        await env.manager.execute("http", "request", {"method": "GET", "url": f"{api.base}/json"})
    ).ok


async def test_a_blocked_destination_is_denied_without_asking_anyone(tmp_path):
    conn = http_connector(tmp_path)
    env, asked = http_env(tmp_path, conn, MANUAL)
    result = await env.manager.execute(
        "http", "request", {"method": "GET", "url": "http://169.254.169.254/latest"}
    )
    assert result.denied and asked == []


# ── Text safety ─────────────────────────────────────────────────────────────


def test_previews_and_descriptions_never_carry_secrets(tmp_path):
    conn = http_connector(tmp_path, trusted=("http://127.0.0.1:5000",))
    actions = decide(
        conn,
        "request",
        {"method": "GET", "url": "http://127.0.0.1:5000/x?api_key=abc123secretvalue&q=1"},
    )[1]
    shown = " ".join(f"{a.target} {a.detail} {a.reason}" for a in actions)
    assert "abc123secretvalue" not in shown and "q=1" in shown
    assert "abc123secretvalue" not in redact_secrets(
        conn.authorization_preview(
            "request", {"method": "GET", "url": "http://127.0.0.1:5000/x?token=abc123secretvalue"}
        )
    )


def test_the_manifest_schemas_contain_no_credentials_or_hosts(tmp_path):
    from velune.resources.capabilities import build_manifest

    save_credential(
        "c1", {"type": "bearer", "origin": "https://api.example.com", "token": "tok-in-schema-99"}
    )
    conn = http_connector(
        tmp_path,
        trusted=("https://api.example.com",),
        environments={"dev": {"base_url": "https://api.example.com"}},
    )
    manifest = build_manifest(conn)
    blob = json.dumps([[o.tool_name, o.description, o.schema] for o in manifest.operations])
    assert "tok-in-schema-99" not in blob and "api.example.com" not in blob
    assert {o.category for o in manifest.operations} == {
        "requests",
        "environments",
        "collections",
        "openapi",
    }
    assert len(manifest.operations) == 11
