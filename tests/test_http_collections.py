"""API collections: format, import/export, execution and per-request authorization (VAIR A3)."""

from __future__ import annotations

import json

import pytest

from tests.http_env import authorized, decide, http_connector, http_env, verdict, write_collection
from tests.http_fakes import isolate_keystore
from tests.http_server import json_reply, start_server, text_reply
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode
from velune.resources.base import ErrorCode
from velune.resources.http.auth import save_credential
from velune.resources.http.collections import (
    FORMAT,
    CollectionError,
    CollectionStore,
    collection_to_dict,
    fingerprint,
    from_postman,
    parse_collection,
    to_postman,
)

MANUAL, AUTO, PLAN = ExecutionMode.MANUAL, ExecutionMode.AUTO, ExecutionMode.PLAN


@pytest.fixture(autouse=True)
def keystore(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path / "keys")


def doc(*requests, **extra):
    base = {"format": FORMAT, "name": "suite", "requests": list(requests)}
    base.update(extra)
    return base


def get(id_, url="/x", **kw):
    return {"id": id_, "method": "GET", "url": url, **kw}


# ── Format ──────────────────────────────────────────────────────────────────


def test_a_collection_round_trips_through_its_stable_format():
    original = doc(
        {
            "id": "login",
            "name": "Log in",
            "method": "POST",
            "url": "{{base_url}}/auth",
            "headers": {"Accept": "application/json"},
            "query": {"v": "1"},
            "body": {"json": {"email": "{{email}}"}},
            "auth": "none",
            "assertions": ["status == 200", {"type": "json", "path": "data.token", "exists": True}],
            "extract": [{"var": "token", "json": "data.token", "secret": True}],
            "timeout": 9,
            "description": "Sign in",
        },
        get("me", "{{base_url}}/me"),
        description="Auth checks",
        environment="dev",
        auth="aidly-dev",
        headers={"Accept": "application/json"},
        variables={"email": "demo@example.com"},
    )
    parsed = parse_collection(original)
    again = parse_collection(collection_to_dict(parsed))
    assert collection_to_dict(again) == collection_to_dict(parsed)
    assert collection_to_dict(parsed)["format"] == FORMAT
    assert [r.id for r in parsed.requests] == ["login", "me"]  # request order is preserved
    assert parsed.requests[0].assertions[0].to_dict() == {"type": "status", "equals": 200}


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ([], "must be a JSON object"),
        (
            {"format": "other/9", "name": "x", "requests": [get("a")]},
            "unsupported collection format",
        ),
        (doc(get("a"), name="Bad Name"), "lowercase"),
        ({"format": FORMAT, "name": "x"}, "non-empty 'requests'"),
        ({"format": FORMAT, "name": "x", "requests": []}, "non-empty 'requests'"),
        (doc(get("a"), get("a")), "duplicate id"),
        (doc({"id": "a", "method": "FETCH", "url": "/x"}), "unsupported method"),
        (doc({"id": "a", "url": ""}), "'url' is required"),
        (doc({"id": "A!", "url": "/x"}), "'id' must be"),
        (doc(get("a", assertions=["bogus"])), "unknown assertion"),
        (doc(get("a", timeout=0)), "'timeout'"),
        (doc(get("a", auth="Bad Ref")), "credential reference"),
        (doc(get("a"), auth="Bad Ref"), "credential reference"),
        (doc(get("a", body={"json": {}, "text": "x", "content_type": "t"})), "exactly one of"),
        (doc(get("a", body={"text": "x"})), "content_type"),
        (doc(get("a", extract=[{"var": "t"}])), "exactly one of 'json' or 'header'"),
        (doc(get("a", extract=[{"var": "bad name", "json": "a"}])), "valid 'var' name"),
        (doc(get("a"), variables={"x": "{{y}}"}), "values are literal"),
        (doc(get("a"), variables={"bad name": "1"}), "not a valid variable name"),
        (doc(get("a", headers={"Host": "evil"})), "cannot be listed"),
    ],
)
def test_malformed_collections_are_rejected_with_a_clear_message(document, fragment):
    with pytest.raises(CollectionError) as caught:
        parse_collection(document)
    assert fragment in str(caught.value)


def test_requests_are_limited():
    with pytest.raises(CollectionError):
        parse_collection(doc(*[get(f"r{i}") for i in range(11)]), max_requests=10)


def test_ids_are_derived_from_names_when_missing():
    parsed = parse_collection(doc({"name": "Get the user list", "url": "/u"}))
    assert parsed.requests[0].id == "get-the-user-list"


@pytest.mark.parametrize("header", ["Authorization", "Cookie", "X-API-Key", "x-auth-token"])
def test_literal_credentials_are_refused_in_a_collection_file(header):
    with pytest.raises(CollectionError) as caught:
        parse_collection(doc(get("a", headers={header: "Bearer literal-secret-value"})))
    assert "credential" in str(caught.value) and "literal-secret-value" not in str(caught.value)


def test_a_credential_header_may_use_a_variable():
    parse_collection(doc(get("a", headers={"Authorization": "Bearer {{token}}"})))


# ── Storage ─────────────────────────────────────────────────────────────────


def test_the_store_lists_loads_and_saves_collections(tmp_path):
    store = CollectionStore(tmp_path / "c")
    assert store.names() == []
    saved = store.save(parse_collection(doc(get("a"))))
    assert saved.name == "suite.json" and store.names() == ["suite"]
    assert store.load("suite").requests[0].id == "a"
    with pytest.raises(CollectionError):
        store.save(parse_collection(doc(get("a"))))  # exists
    store.save(parse_collection(doc(get("b"))), overwrite=True)
    assert store.load("suite").requests[0].id == "b"


@pytest.mark.parametrize("name", ["../x", "a/b", "A", "", "a" * 60, "..", "a.b"])
def test_collection_names_cannot_escape_the_directory(tmp_path, name):
    with pytest.raises(CollectionError):
        CollectionStore(tmp_path).path_for(name)


def test_the_store_reports_missing_corrupt_and_oversized_files(tmp_path):
    store = CollectionStore(tmp_path)
    with pytest.raises(CollectionError, match="no collection"):
        store.load("ghost")
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(CollectionError, match="JSON"):
        store.load("bad")
    (tmp_path / "big.json").write_text(" " * (1024 * 1024 + 10), encoding="utf-8")
    with pytest.raises(CollectionError, match="too large"):
        store.load("big")


def test_the_fingerprint_changes_with_the_collection_and_its_context():
    base = parse_collection(doc(get("a")))
    assert fingerprint(base, "dev") == fingerprint(parse_collection(doc(get("a"))), "dev")
    assert fingerprint(base, "dev") != fingerprint(base, "prod")
    assert fingerprint(base) != fingerprint(parse_collection(doc(get("a", "/other"))))


# ── Postman v2.1 subset ─────────────────────────────────────────────────────

POSTMAN = {
    "info": {
        "name": "Aidly API",
        "description": "Imported",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "variable": [{"key": "version", "value": "v1"}, {"key": "tpl", "value": "{{x}}"}],
    "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{token}}"}]},
    "item": [
        {
            "name": "Auth",
            "item": [
                {
                    "name": "Login",
                    "request": {
                        "method": "POST",
                        "header": [
                            {"key": "Content-Type", "value": "application/json"},
                            {"key": "Authorization", "value": "Bearer literal-secret-1"},
                            {"key": "X-Off", "value": "no", "disabled": True},
                        ],
                        "url": {
                            "raw": "{{base_url}}/auth/login?a=1",
                            "query": [{"key": "a", "value": "1"}],
                        },
                        "body": {
                            "mode": "raw",
                            "raw": '{"email": "a@b.c"}',
                            "options": {"raw": {"language": "json"}},
                        },
                    },
                    "event": [{"listen": "test", "script": {"exec": ["pm.test('x')"]}}],
                },
                {
                    "name": "Logout",
                    "request": {"method": "DELETE", "url": "{{base_url}}/auth/session"},
                },
            ],
        },
        {
            "name": "Upload",
            "request": {
                "method": "POST",
                "url": {"raw": "{{base_url}}/up"},
                "body": {
                    "mode": "formdata",
                    "formdata": [
                        {"key": "title", "value": "T", "type": "text"},
                        {"key": "doc", "type": "file", "src": "/etc/passwd"},
                    ],
                },
            },
        },
        {
            "name": "Form",
            "request": {
                "method": "POST",
                "url": "{{base_url}}/f",
                "body": {"mode": "urlencoded", "urlencoded": [{"key": "a", "value": "1"}]},
            },
        },
    ],
}


def test_postman_import_maps_the_supported_subset_and_lists_what_it_skipped():
    imported = from_postman(POSTMAN)
    collection = imported.collection
    assert collection.name == "aidly-api"
    assert [r.id for r in collection.requests] == [
        "auth-login",
        "auth-logout",
        "upload",
        "form",
    ]  # folders flattened
    login = collection.request("auth-login")
    assert login.method == "POST" and login.url == "{{base_url}}/auth/login"
    assert dict(login.query) == {"a": "1"} and login.body == {"json": {"email": "a@b.c"}}
    assert [h for h, _ in login.headers] == [
        "Content-Type"
    ]  # disabled and literal-credential headers dropped
    assert collection.request("upload").body == {
        "multipart": {"title": "T"}
    }  # file fields are not imported
    assert collection.request("form").body == {"form": {"a": "1"}}
    assert dict(collection.variables) == {"version": "v1"}  # a templated variable value is skipped
    skipped = " ".join(imported.skipped)
    for expected in (
        "scripts on 'Auth / Login'",
        "collection-level auth",
        "literal 'Authorization' header",
        "file field 'doc'",
    ):
        assert expected in skipped
    assert "literal-secret-1" not in json.dumps(collection_to_dict(collection))


@pytest.mark.parametrize(
    "document",
    [
        {},
        [],
        {"item": "x"},
        {"item": []},
        {"info": {"schema": "v1.0"}, "item": [{"name": "r", "request": {}}]},
    ],
)
def test_non_postman_or_empty_documents_are_refused(document):
    with pytest.raises(CollectionError):
        from_postman(document)


def test_postman_export_is_valid_v21_and_documented_as_lossy():
    collection = parse_collection(
        doc(
            {
                "id": "a",
                "method": "POST",
                "url": "{{base_url}}/a",
                "query": {"q": "1"},
                "headers": {"X-A": "1"},
                "body": {"json": {"k": 1}},
                "assertions": ["status == 200"],
            },
            variables={"v": "1"},
        )
    )
    exported = to_postman(collection)
    assert "v2.1.0" in exported["info"]["schema"]
    request = exported["item"][0]["request"]
    assert request["method"] == "POST" and request["url"]["raw"] == "{{base_url}}/a?q=1"
    assert (
        json.loads(request["body"]["raw"]) == {"k": 1}
        and request["body"]["options"]["raw"]["language"] == "json"
    )
    assert exported["variable"] == [{"key": "v", "value": "1"}]
    assert "assertions" not in json.dumps(exported)  # not exported: documented limitation


def test_postman_round_trip_keeps_requests_methods_and_urls():
    again = from_postman(to_postman(from_postman(POSTMAN).collection)).collection
    original = from_postman(POSTMAN).collection
    assert [(r.method, r.url) for r in again.requests] == [
        (r.method, r.url) for r in original.requests
    ]


# ── Running collections ─────────────────────────────────────────────────────


@pytest.fixture
async def api():
    def handler(seen):
        if seen.path == "/login":
            body = json.loads(seen.body or b"{}")
            if body.get("password") == "right-password":
                return json_reply('{"data": {"token": "session-token-abc123", "id": 1}}')
            return text_reply("bad credentials", 401)
        if seen.path == "/me":
            if seen.headers.get("authorization") == "Bearer session-token-abc123":
                return json_reply('{"data": {"id": 1, "name": "Ada"}}')
            return text_reply("nope", 401)
        if seen.path == "/boom":
            return text_reply("kaboom", 500)
        if seen.method == "DELETE":
            return 204, {}, b""
        return json_reply('{"ok": true}')

    server = await start_server(handler)
    yield server
    await server.stop()


def connector(tmp_path, api, **kw):
    return http_connector(
        tmp_path,
        trusted=(api.base,),
        environments={"dev": {"base_url": api.base, "secret_variables": {"pw": "demo-password"}}},
        **kw,
    )


async def run(conn, name="suite", **params):
    return await authorized(conn, "collection_run", {"name": name, **params})


async def test_requests_run_in_order_with_assertions_and_extracted_variables(tmp_path, api):
    save_credential("demo-password", {"type": "secret", "value": "right-password"})
    write_collection(
        tmp_path,
        doc(
            {
                "id": "login",
                "method": "POST",
                "url": "{{base_url}}/login",
                "body": {"json": {"password": "{{pw}}"}},
                "assertions": ["status == 200", "json data.token exists"],
                "extract": [{"var": "token", "json": "data.token", "secret": True}],
            },
            {
                "id": "me",
                "method": "GET",
                "url": "{{base_url}}/me",
                "headers": {"Authorization": "Bearer {{token}}"},
                "assertions": ["status == 200", "json data.name == Ada"],
            },
            environment="dev",
        ),
    )
    result = await run(connector(tmp_path, api))
    assert result.ok
    data = result.data
    assert (data["total"], data["ran"], data["passed"], data["failed"]) == (2, 2, 2, 0) and data[
        "all_passed"
    ]
    assert [r["id"] for r in data["results"]] == ["login", "me"]
    assert [r.path for r in api.requests] == ["/login", "/me"]  # sequential, in declared order
    assert api.requests[1].headers["authorization"] == "Bearer session-token-abc123"
    assert "session-token-abc123" not in json.dumps(
        result.to_dict()
    )  # extracted secrets never surface


async def test_failed_requests_are_summarized_and_the_run_continues(tmp_path, api):
    write_collection(
        tmp_path,
        doc(
            {
                "id": "ok",
                "method": "GET",
                "url": "{{base_url}}/fine",
                "assertions": ["status == 200"],
            },
            {
                "id": "wrong",
                "method": "GET",
                "url": "{{base_url}}/fine",
                "assertions": ["status == 201", "json ok == true"],
            },
            {"id": "server", "method": "GET", "url": "{{base_url}}/boom"},
            {"id": "dead", "method": "GET", "url": "{{base_url}}/{{missing}}"},
            {"id": "last", "method": "GET", "url": "{{base_url}}/fine"},
            environment="dev",
        ),
    )
    data = (await run(connector(tmp_path, api))).data
    assert (data["ran"], data["passed"], data["failed"], data["errors"]) == (5, 2, 2, 1)
    assert not data["all_passed"]
    by_id = {r["id"]: r for r in data["failed_requests"]}
    assert set(by_id) == {"wrong", "server", "dead"}
    assert by_id["wrong"]["failed"][0]["assertion"] == "status == 201"
    assert by_id["server"]["outcome"] == "server_error"
    assert "unknown variable" in by_id["dead"]["error"]
    assert [r["id"] for r in data["results"]][-1] == "last"  # it kept going


async def test_stop_on_failure_halts_at_the_first_failed_request(tmp_path, api):
    write_collection(
        tmp_path,
        doc(
            get("a", "{{base_url}}/fine"),
            get("b", "{{base_url}}/boom", assertions=["status == 200"]),
            get("c", "{{base_url}}/fine"),
            environment="dev",
        ),
    )
    data = (await run(connector(tmp_path, api), stop_on_failure=True)).data
    assert data["ran"] == 2 and data["total"] == 3 and not data["all_passed"]


async def test_a_single_request_can_be_run_and_an_unknown_one_is_reported(tmp_path, api):
    write_collection(
        tmp_path,
        doc(get("a", "{{base_url}}/fine"), get("b", "{{base_url}}/fine"), environment="dev"),
    )
    conn = connector(tmp_path, api)
    single = (await run(conn, request="b")).data["results"][0]
    assert (
        single["id"] == "b" and single["response"]["status"] == 200
    )  # the whole response is available
    assert "response" not in (await run(conn)).data["results"][0]  # full runs stay compact
    missing = await authorized(conn, "collection_run", {"name": "suite", "request": "zzz"})
    assert (
        not missing.ok and missing.code is ErrorCode.NOT_FOUND and "Requests: a, b" in missing.error
    )


async def test_a_missing_collection_is_not_found(tmp_path, api):
    result = await run(connector(tmp_path, api), name="ghost")
    assert not result.ok and result.code is ErrorCode.NOT_FOUND


async def test_collections_inspect_without_secret_values(tmp_path, api):
    write_collection(
        tmp_path,
        doc(
            get("a", "{{base_url}}/x", assertions=["status == 200"]),
            description="Smoke checks",
            environment="dev",
        ),
    )
    conn = connector(tmp_path, api)
    listing = await conn.execute("collection_list", {})
    assert listing.data["collections"] == [
        {"name": "suite", "requests": 1, "description": "Smoke checks"}
    ]
    shown = await conn.execute("collection_show", {"name": "suite"})
    assert shown.data["requests"][0]["assertions"] == [{"type": "status", "equals": 200}]
    assert not (await conn.execute("collection_show", {"name": "ghost"})).ok


# ── A collection is not a way around approval ───────────────────────────────


def mixed(tmp_path):
    write_collection(
        tmp_path,
        doc(
            get("read", "{{base_url}}/fine"),
            {"id": "write", "method": "POST", "url": "{{base_url}}/fine", "body": {"json": {}}},
            {"id": "gone", "method": "DELETE", "url": "{{base_url}}/thing/1"},
            environment="dev",
        ),
    )


def test_the_authorization_covers_every_request_in_the_collection(tmp_path, api):
    mixed(tmp_path)
    conn = connector(tmp_path, api)
    _decision, actions = decide(conn, "collection_run", {"name": "suite"})
    assert {a.scope for a in actions} >= {
        "http.request.read",
        "http.request.write",
        "http.request.delete",
    }
    assert len({(a.scope, a.target) for a in actions}) == 3


@pytest.mark.parametrize(("mode", "expected"), [(MANUAL, "ask"), (AUTO, "ask"), (PLAN, "deny")])
def test_a_collection_with_a_delete_is_gated_like_that_delete(tmp_path, api, mode, expected):
    mixed(tmp_path)
    assert verdict(connector(tmp_path, api), "collection_run", {"name": "suite"}, mode) == expected


def test_a_read_only_collection_is_allowed_everywhere_it_is_trusted(tmp_path, api):
    write_collection(
        tmp_path,
        doc(get("a", "{{base_url}}/fine"), get("b", "{{base_url}}/fine"), environment="dev"),
    )
    conn = connector(tmp_path, api)
    for mode in (MANUAL, AUTO, PLAN):
        assert verdict(conn, "collection_run", {"name": "suite"}, mode) == "allow"


def test_a_collection_that_reaches_a_blocked_destination_is_denied_as_a_whole(tmp_path, api):
    write_collection(
        tmp_path,
        doc(
            get("a", "{{base_url}}/fine"),
            get("b", "http://169.254.169.254/latest"),
            environment="dev",
        ),
    )
    for mode in (MANUAL, AUTO, PLAN):
        assert (
            verdict(connector(tmp_path, api), "collection_run", {"name": "suite"}, mode) == "deny"
        )


def test_a_collection_cannot_grant_trust_to_the_origins_it_names(tmp_path):
    for field in ("trusted_origins", "extra_origins", "allow_private_network", "permissions"):
        with pytest.raises(CollectionError, match="unknown collection field"):
            parse_collection(
                {
                    **doc(get("a", "https://remote.example.com/x")),
                    field: ["https://remote.example.com"],
                }
            )
    write_collection(tmp_path, doc(get("a", "https://remote.example.com/x")))
    # Remote destinations still need a network confirmation, whatever the file says.
    assert verdict(http_connector(tmp_path), "collection_run", {"name": "suite"}, MANUAL) == "ask"


def test_unknown_request_fields_are_refused_too():
    with pytest.raises(CollectionError, match="unknown field"):
        parse_collection(doc({"id": "a", "url": "/x", "trust": True}))


async def test_a_run_through_the_gate_asks_once_and_then_runs_everything(tmp_path, api):
    write_collection(
        tmp_path,
        doc(
            get("read", "{{base_url}}/fine"),
            {"id": "gone", "method": "DELETE", "url": "{{base_url}}/thing/1"},
            environment="dev",
        ),
    )
    env, asked = http_env(tmp_path, connector(tmp_path, api), MANUAL)
    result = await env.manager.execute("http", "collection_run", {"name": "suite"})
    assert result.ok and result.data["ran"] == 2 and len(asked) == 1
    assert "DELETE" in asked[0][1].reason or any("DELETE" in a.target for a in asked[0][1].actions)


async def test_a_denied_collection_run_sends_nothing(tmp_path, api):
    mixed(tmp_path)
    env, _asked = http_env(tmp_path, connector(tmp_path, api), MANUAL, answer=Approval.DENY)
    result = await env.manager.execute("http", "collection_run", {"name": "suite"})
    assert result.denied and api.requests == []


async def test_plan_mode_refuses_a_collection_with_changes_before_sending_any_request(
    tmp_path, api
):
    mixed(tmp_path)
    env, asked = http_env(tmp_path, connector(tmp_path, api), PLAN)
    result = await env.manager.execute("http", "collection_run", {"name": "suite"})
    assert result.denied and api.requests == [] and asked == []  # not even the read ran


async def test_a_collection_edited_after_approval_is_refused(tmp_path, api):
    write_collection(tmp_path, doc(get("a", "{{base_url}}/fine"), environment="dev"))
    conn = connector(tmp_path, api)
    conn.describe_actions("collection_run", {"name": "suite"})  # approval is computed here...
    write_collection(
        tmp_path,
        doc(
            get("a", "{{base_url}}/fine"),
            {"id": "gone", "method": "DELETE", "url": "{{base_url}}/thing/1"},
            environment="dev",
        ),
    )
    result = await conn.execute("collection_run", {"name": "suite"})  # ...but the file changed
    assert not result.ok and result.code is ErrorCode.AUTHORIZATION_DENIED and api.requests == []


async def test_running_a_collection_that_was_never_described_is_refused(tmp_path, api):
    write_collection(tmp_path, doc(get("a", "{{base_url}}/fine"), environment="dev"))
    result = await connector(tmp_path, api).execute("collection_run", {"name": "suite"})
    assert not result.ok and result.code is ErrorCode.AUTHORIZATION_DENIED and api.requests == []


async def test_a_collection_run_does_not_widen_which_destinations_are_reachable(tmp_path, api):
    write_collection(
        tmp_path,
        doc(get("a", "{{base_url}}/fine"), get("b", "http://localhost:1/x"), environment="dev"),
    )
    conn = http_connector(
        tmp_path,
        trusted=(api.base,),
        allow_loopback=True,
        environments={"dev": {"base_url": api.base}},
        connect_timeout=10,
    )
    data = (await run(conn)).data
    assert (
        data["passed"] == 1 and data["errors"] == 1
    )  # the second request hit the network, not a shortcut


# ── Import and export operations ────────────────────────────────────────────


async def test_collections_are_imported_from_workspace_files(tmp_path, api):
    (tmp_path / "pm.json").write_text(json.dumps(POSTMAN), encoding="utf-8")
    (tmp_path / "mine.json").write_text(json.dumps(doc(get("a"))), encoding="utf-8")
    conn = connector(tmp_path, api)
    result = await conn.execute(
        "collection_import", {"file": "pm.json", "format": "postman", "name": "aidly"}
    )
    assert result.ok and result.data["name"] == "aidly" and result.data["requests"] == 4
    assert result.data["saved_to"].endswith("aidly.json") and result.data["skipped"]
    native = await conn.execute("collection_import", {"file": "mine.json", "name": "renamed"})
    assert native.ok and native.data["name"] == "renamed"
    assert sorted(CollectionStore(tmp_path / ".velune" / "api" / "collections").names()) == [
        "aidly",
        "renamed",
    ]
    again = await conn.execute("collection_import", {"file": "mine.json", "name": "renamed"})
    assert not again.ok and "already exists" in again.error
    assert (
        await conn.execute(
            "collection_import", {"file": "mine.json", "name": "renamed", "overwrite": True}
        )
    ).ok


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"file": "missing.json"}, ErrorCode.NOT_FOUND),
        ({"file": "../outside.json"}, ErrorCode.VALIDATION_FAILED),
        ({"file": "bad.json"}, ErrorCode.VALIDATION_FAILED),
        ({"file": "nonpostman.json", "format": "postman"}, ErrorCode.VALIDATION_FAILED),
        ({"file": "mine.json", "name": "Bad Name"}, ErrorCode.VALIDATION_FAILED),
    ],
)
async def test_bad_imports_fail_cleanly(tmp_path, api, params, code):
    (tmp_path.parent / "outside.json").write_text("{}", encoding="utf-8")
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    (tmp_path / "nonpostman.json").write_text('{"hello": 1}', encoding="utf-8")
    (tmp_path / "mine.json").write_text(json.dumps(doc(get("a"))), encoding="utf-8")
    result = await connector(tmp_path, api).execute("collection_import", params)
    assert not result.ok and result.code is code


async def test_collections_are_exported_in_both_formats(tmp_path, api):
    write_collection(
        tmp_path, doc(get("a", "{{base_url}}/x", assertions=["status == 200"]), environment="dev")
    )
    conn = connector(tmp_path, api)
    native = await conn.execute("collection_export", {"name": "suite", "file": "out/suite.json"})
    assert (
        native.ok
        and parse_collection(json.loads((tmp_path / "out" / "suite.json").read_text()))
        .requests[0]
        .id
        == "a"
    )
    postman = await conn.execute(
        "collection_export", {"name": "suite", "file": "pm.json", "format": "postman"}
    )
    assert postman.ok and "omits assertions" in postman.data["note"]
    assert "v2.1.0" in json.loads((tmp_path / "pm.json").read_text())["info"]["schema"]


@pytest.mark.parametrize(
    ("file", "code"),
    [
        ("../escape.json", ErrorCode.VALIDATION_FAILED),
        ("out.txt", ErrorCode.VALIDATION_FAILED),
        (".git/config.json", ErrorCode.VALIDATION_FAILED),
        (".env.json", ErrorCode.VALIDATION_FAILED),
    ],
)
async def test_exports_are_confined_to_safe_workspace_destinations(tmp_path, api, file, code):
    write_collection(tmp_path, doc(get("a"), environment="dev"))
    result = await connector(tmp_path, api).execute(
        "collection_export", {"name": "suite", "file": file}
    )
    assert not result.ok and result.code is code


async def test_an_export_does_not_overwrite_without_being_told_to(tmp_path, api):
    write_collection(tmp_path, doc(get("a"), environment="dev"))
    (tmp_path / "exists.json").write_text("{}", encoding="utf-8")
    conn = connector(tmp_path, api)
    refused = await conn.execute("collection_export", {"name": "suite", "file": "exists.json"})
    assert not refused.ok and refused.code is ErrorCode.CONFLICT
    assert (
        await conn.execute(
            "collection_export", {"name": "suite", "file": "exists.json", "overwrite": True}
        )
    ).ok


def test_import_and_export_are_gated_as_writes_with_outside_paths_flagged(tmp_path, api):
    conn = connector(tmp_path, api)
    assert verdict(conn, "collection_import", {"file": "a.json"}, PLAN) == "deny"
    assert verdict(conn, "collection_import", {"file": "a.json"}, MANUAL) == "ask"
    assert (
        verdict(conn, "collection_export", {"name": "suite", "file": "../x.json"}, AUTO) == "deny"
    )
    assert verdict(conn, "collection_list", {}, PLAN) == "allow"


def test_collections_directory_outside_the_workspace_is_refused(tmp_path):
    conn = http_connector(tmp_path, collections_dir="../elsewhere")
    assert conn._store is None
    assert conn.configuration_problem() is None
