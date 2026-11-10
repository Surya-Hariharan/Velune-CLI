"""OpenAPI 3.x: parsing untrusted specs, endpoint search, validated calls (VAIR A3)."""

from __future__ import annotations

import json
import time

import pytest

from tests.http_env import authorized, http_connector, verdict
from tests.http_fakes import isolate_keystore
from tests.http_server import json_reply, start_server
from velune.permissions.policy import ExecutionMode
from velune.resources.base import ErrorCode
from velune.resources.http import openapi as oa

MANUAL, AUTO, PLAN = ExecutionMode.MANUAL, ExecutionMode.AUTO, ExecutionMode.PLAN


@pytest.fixture(autouse=True)
def keystore(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path / "keys")


def spec(**overrides):
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Aidly API", "version": "1.2.0"},
        "servers": [
            {
                "url": "{scheme}://api.example.com/{base}",
                "variables": {"scheme": {"default": "https"}, "base": {"default": "v1"}},
            }
        ],
        "security": [{"bearer": []}],
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "summary": "List users",
                    "tags": ["users"],
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        {
                            "name": "status",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string", "enum": ["active", "retired"]},
                        },
                    ],
                    "responses": {"200": {"description": "A page of users"}},
                },
                "post": {
                    "operationId": "createUser",
                    "summary": "Create a user",
                    "tags": ["users"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/NewUser"}}
                        },
                    },
                    "responses": {
                        "201": {"description": "Created"},
                        "422": {"description": "Invalid"},
                    },
                },
            },
            "/users/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                "get": {
                    "operationId": "getUser",
                    "summary": "Fetch one user",
                    "tags": ["users"],
                    "responses": {"200": {"description": "A user"}},
                },
                "delete": {
                    "operationId": "deleteUser",
                    "summary": "Delete a user",
                    "tags": ["users"],
                    "security": [{"bearer": []}],
                    "responses": {"204": {"description": "Gone"}},
                },
            },
            "/health": {
                "get": {
                    "summary": "Liveness",
                    "security": [],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
        "components": {
            "schemas": {
                "NewUser": {
                    "type": "object",
                    "required": ["email", "age"],
                    "properties": {
                        "email": {"type": "string", "minLength": 5, "maxLength": 50},
                        "age": {"type": "integer", "minimum": 18},
                        "role": {"type": "string", "enum": ["admin", "member"]},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                }
            },
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        },
    }
    document.update(overrides)
    return document


def parse(document=None, **kw):
    return oa.parse_spec(json.dumps(document or spec()), name="aidly", source="test.json", **kw)


# ── Parsing ─────────────────────────────────────────────────────────────────


def test_endpoints_methods_parameters_bodies_and_responses_are_discovered():
    catalog = parse()
    assert set(catalog.endpoints) == {
        "listUsers",
        "createUser",
        "getUser",
        "deleteUser",
        "get_health",
    }
    users = catalog.endpoints["listUsers"]
    assert (users.method, users.path, users.summary, users.tags) == (
        "GET",
        "/users",
        "List users",
        ("users",),
    )
    by_name = {p.name: p for p in users.parameters}
    assert by_name["status"].required and by_name["status"].schema["enum"] == ["active", "retired"]
    assert by_name["limit"].schema["maximum"] == 100 and not by_name["limit"].required
    create = catalog.endpoints["createUser"]
    assert create.body_content_type == "application/json" and create.body_required
    assert create.body_schema["required"] == ["email", "age"]  # the $ref was resolved
    assert create.responses == {"201": "Created", "422": "Invalid"}
    delete = catalog.endpoints["deleteUser"]
    assert [p.name for p in delete.parameters] == ["id"] and delete.parameters[
        0
    ].location == "path"  # path-level parameter inherited


def test_authentication_requirements_are_reported_per_operation():
    catalog = parse()
    assert catalog.endpoints["getUser"].security == ("bearer",)  # inherited from the document
    assert catalog.endpoints["get_health"].security == ()  # explicitly public
    assert catalog.security_schemes == {"bearer": {"type": "http", "scheme": "bearer"}}
    detail = oa.endpoint_detail(catalog.endpoints["getUser"], catalog)
    assert detail["security"] == [{"name": "bearer", "type": "http", "scheme": "bearer"}]


def test_servers_are_resolved_with_their_default_variables_and_titles_recorded():
    catalog = parse()
    assert catalog.servers == ["https://api.example.com/v1"]
    assert (catalog.title, catalog.version) == ("Aidly API", "1.2.0")
    summary = catalog.summary()
    assert summary["endpoints"] == 5 and summary["methods"] == {"DELETE": 1, "GET": 3, "POST": 1}


def test_missing_operation_ids_are_generated_and_duplicates_made_unique():
    document = spec(
        paths={
            "/a/b": {"get": {"responses": {}}},
            "/c": {"get": {"operationId": "same"}, "post": {"operationId": "same"}},
        }
    )
    assert set(parse(document).endpoints) == {"get_a_b", "same", "same_2"}


def test_undeclared_path_variables_become_required_parameters():
    document = spec(paths={"/things/{thingId}": {"get": {"operationId": "t", "responses": {}}}})
    parameter = parse(document).endpoints["t"].parameters[0]
    assert (parameter.name, parameter.location, parameter.required) == ("thingId", "path", True)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("not json at all {", "must be an object"),  # plain text is a YAML scalar, not a document
        ('{"openapi": "3.0.0"}', "no 'paths'"),
        ('{"swagger": "2.0", "paths": {"/": {}}}', "Swagger 2.0"),
        ('{"paths": {"/": {}}}', "not an OpenAPI 3.x"),
        ("[1, 2]", "must be an object"),
        ('{"openapi": "3.0.0", "paths": {"/x": {"trace": {}}}}', "no operations"),
        ('{"openapi": "3.0.0", "paths": {', "not valid JSON"),
    ],
)
def test_invalid_specifications_are_rejected_with_a_clear_message(text, fragment):
    pytest.importorskip("yaml")
    with pytest.raises(oa.OpenApiError, match=fragment):
        oa.parse_spec(text, name="x", source="x")


def test_yaml_specifications_are_parsed_with_safe_load():
    pytest.importorskip("yaml")
    text = "openapi: 3.0.0\ninfo: {title: Y, version: '1'}\npaths:\n  /ping:\n    get:\n      operationId: ping\n      responses: {}\n"
    assert "ping" in oa.parse_spec(text, name="y", source="y.yaml").endpoints


def test_yaml_cannot_construct_arbitrary_objects():
    pytest.importorskip("yaml")
    evil = "openapi: 3.0.0\npaths: !!python/object/apply:os.getcwd []\n"
    with pytest.raises(oa.OpenApiError):
        oa.parse_spec(evil, name="y", source="evil.yaml")


def test_yaml_without_the_extra_installed_says_how_to_get_it(monkeypatch):
    import builtins

    real = builtins.__import__

    def deny_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_yaml)
    with pytest.raises(oa.OpenApiError, match=r"velune-cli\[api\]"):
        oa.load_document("openapi: 3.0.0\npaths: {}\n")


# ── Untrusted-input hardening ───────────────────────────────────────────────


def test_circular_references_terminate_and_are_marked():
    document = spec(
        components={
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                }
            }
        }
    )
    document["paths"] = {
        "/n": {
            "post": {
                "operationId": "n",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}
                    }
                },
                "responses": {},
            }
        }
    }
    catalog = parse(document)
    assert "recursive" in json.dumps(catalog.endpoints["n"].body_schema)


def test_an_exponentially_expanding_document_stays_bounded():
    schemas = {"L0": {"type": "string"}}
    for i in range(1, 30):
        schemas[f"L{i}"] = {
            "type": "object",
            "properties": {
                "a": {"$ref": f"#/components/schemas/L{i - 1}"},
                "b": {"$ref": f"#/components/schemas/L{i - 1}"},
            },
        }
    document = spec(components={"schemas": schemas})
    document["paths"] = {
        "/bomb": {
            "post": {
                "operationId": "bomb",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/L29"}}
                    }
                },
                "responses": {},
            }
        }
    }
    started = time.perf_counter()
    catalog = parse(document)
    assert time.perf_counter() - started < 5  # 2**29 leaves were not materialized
    assert (
        len(json.dumps(catalog.endpoints["bomb"].body_schema)) < 5000
    )  # and what is shown is compact


def test_external_and_dangling_references_are_not_followed():
    document = spec()
    document["paths"]["/ext"] = {
        "get": {
            "operationId": "ext",
            "parameters": [
                {"$ref": "https://evil.example/p.json"},
                {"$ref": "#/components/missing"},
            ],
            "responses": {"200": {"$ref": "file:///etc/passwd"}},
        }
    }
    catalog = parse(document)
    assert catalog.endpoints["ext"].parameters == ()
    assert "external references are not followed" in catalog.warnings


def test_oversized_specifications_are_refused():
    with pytest.raises(oa.OpenApiError, match="larger than"):
        oa.load_document("x" * (oa.MAX_SPEC_BYTES + 10))


def test_a_huge_catalog_is_bounded_and_searchable():
    paths = {
        f"/r{i}/items": {
            "get": {
                "operationId": f"list{i}",
                "summary": f"List resource {i}",
                "tags": [f"t{i % 10}"],
                "responses": {},
            }
        }
        for i in range(6000)
    }
    started = time.perf_counter()
    catalog = parse(spec(paths=paths))
    assert len(catalog.endpoints) == oa.MAX_ENDPOINTS and any(
        "first" in w for w in catalog.warnings
    )
    assert time.perf_counter() - started < 10
    assert len(oa.search(catalog, "list resource", limit=5)) == 5


def test_descriptions_from_a_spec_are_bounded_text():
    document = spec(
        paths={
            "/x": {
                "get": {
                    "operationId": "x",
                    "summary": "S" * 5000,
                    "description": "D" * 5000,
                    "responses": {},
                }
            }
        }
    )
    endpoint = parse(document).endpoints["x"]
    assert len(endpoint.summary) <= 160 and len(endpoint.description) <= 300


# ── Search ──────────────────────────────────────────────────────────────────


def test_search_ranks_whole_word_matches_and_filters():
    catalog = parse()
    assert [e.operation_id for e in oa.search(catalog, "delete user")][0] == "deleteUser"
    assert {e.operation_id for e in oa.search(catalog, "", tag="users")} == {
        "listUsers",
        "createUser",
        "getUser",
        "deleteUser",
    }
    assert [e.operation_id for e in oa.search(catalog, "", method="DELETE")] == ["deleteUser"]
    assert oa.search(catalog, "nonexistent zzz") == []
    assert len(oa.search(catalog, "", limit=2)) == 2
    assert oa.search(catalog, "user") == oa.search(catalog, "user")  # deterministic


# ── Validation against the spec ─────────────────────────────────────────────


def check(operation, **kw):
    kw.setdefault("path", {})
    kw.setdefault("query", {})
    kw.setdefault("headers", {})
    kw.setdefault("body", None)
    kw.setdefault("has_body", kw["body"] is not None)
    return oa.check_call(parse().endpoints[operation], **kw)


def test_a_consistent_call_has_no_problems():
    assert check("listUsers", query={"status": "active", "limit": "10"}) == []
    assert check("createUser", body={"email": "a@b.co", "age": 30, "tags": ["x"]}) == []
    assert check("getUser", path={"id": "7"}) == []


@pytest.mark.parametrize(
    ("operation", "kwargs", "fragment"),
    [
        ("listUsers", {}, "'status' is required"),
        ("listUsers", {"query": {"status": "unknown"}}, "not one of the allowed values"),
        ("listUsers", {"query": {"status": "active", "limit": "1000"}}, "above the maximum 100"),
        ("listUsers", {"query": {"status": "active", "limit": "abc"}}, "expected integer"),
        ("getUser", {}, "path parameter 'id' is required"),
        ("getUser", {"path": {"id": "seven"}}, "expected integer"),
        ("createUser", {}, "a request body is required"),
        ("createUser", {"body": {"email": "a@b.co"}}, "body.age: required"),
        ("createUser", {"body": {"email": "x", "age": 30}}, "shorter than 5"),
        ("createUser", {"body": {"email": "a@b.co", "age": 5}}, "below the minimum 18"),
        ("createUser", {"body": {"email": "a@b.co", "age": "old"}}, "expected integer"),
        (
            "createUser",
            {"body": {"email": "a@b.co", "age": 30, "role": "root"}},
            "not one of the allowed values",
        ),
        ("createUser", {"body": {"email": "a@b.co", "age": 30, "tags": [1]}}, "expected string"),
        (
            "createUser",
            {"body": {"email": "a@b.co", "age": True}},
            "expected integer",
        ),  # a boolean is not an integer
    ],
)
def test_inconsistent_calls_are_reported_without_echoing_values(operation, kwargs, fragment):
    problems = check(operation, **kwargs)
    assert any(fragment in p for p in problems)
    assert "root" not in " ".join(problems) and "abc" not in " ".join(problems).replace("above", "")


def test_path_variables_are_percent_encoded():
    assert oa.fill_path("/files/{name}/x", {"name": "a b/../c"}) == "/files/a%20b%2F..%2Fc/x"


# ── The connector operations ────────────────────────────────────────────────


@pytest.fixture
async def api():
    server = await start_server(
        lambda s: json_reply(
            json.dumps({"method": s.method, "target": s.target, "body": s.body.decode()})
        )
    )
    yield server
    await server.stop()


def connector(tmp_path, api, document=None):
    (tmp_path / "openapi.json").write_text(json.dumps(document or spec()), encoding="utf-8")
    return http_connector(
        tmp_path, trusted=(api.base,), environments={"dev": {"base_url": api.base}}
    )


async def test_a_local_specification_is_loaded_searched_and_called(tmp_path, api):
    conn = connector(tmp_path, api)
    loaded = await conn.execute("openapi_load", {"file": "openapi.json", "name": "aidly"})
    assert (
        loaded.ok
        and loaded.data["endpoints"] == 5
        and loaded.data["security_schemes"] == {"bearer": "http"}
    )
    found = await conn.execute("openapi_search", {"spec": "aidly", "query": "create user"})
    assert found.data["matches"][0] == {
        "operation": "createUser",
        "method": "POST",
        "path": "/users",
        "summary": "Create a user",
        "tags": ["users"],
    }
    detail = await conn.execute("openapi_search", {"spec": "aidly", "operation": "createUser"})
    assert detail.data["endpoint"]["request_body"]["required"] is True
    created = await authorized(
        conn,
        "openapi_call",
        {"spec": "aidly", "operation": "createUser", "json": '{"email": "a@b.co", "age": 31}'},
    )
    assert (
        created.ok
        and created.data["json"]["method"] == "POST"
        and created.data["json"]["target"] == "/users"
    )
    got = await authorized(
        conn,
        "openapi_call",
        {
            "spec": "aidly",
            "operation": "getUser",
            "path_params": ["id=42"],
            "expect": ["status == 200"],
        },
    )
    assert got.data["json"]["target"] == "/users/42" and got.data["assertions"]["passed"]


async def test_calls_that_violate_the_specification_are_refused_before_sending(tmp_path, api):
    conn = connector(tmp_path, api)
    await conn.execute("openapi_load", {"file": "openapi.json", "name": "aidly"})
    result = await authorized(
        conn,
        "openapi_call",
        {"spec": "aidly", "operation": "createUser", "json": '{"email": "a@b.co"}'},
    )
    assert (
        not result.ok
        and result.code is ErrorCode.VALIDATION_FAILED
        and "body.age: required" in result.error
    )
    assert api.requests == []


async def test_unknown_specs_and_operations_are_reported(tmp_path, api):
    conn = connector(tmp_path, api)
    assert (await conn.execute("openapi_search", {"spec": "ghost"})).code is ErrorCode.NOT_FOUND
    await conn.execute("openapi_load", {"file": "openapi.json", "name": "aidly"})
    missing = await authorized(conn, "openapi_call", {"spec": "aidly", "operation": "nuke"})
    assert (
        not missing.ok and missing.code is ErrorCode.NOT_FOUND and "openapi_search" in missing.hint
    )


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, ErrorCode.VALIDATION_FAILED),
        ({"file": "a", "url": "http://x/"}, ErrorCode.VALIDATION_FAILED),
        ({"file": "missing.json"}, ErrorCode.NOT_FOUND),
        ({"file": "../outside.json"}, ErrorCode.VALIDATION_FAILED),
        ({"file": "broken.json"}, ErrorCode.VALIDATION_FAILED),
        ({"file": "openapi.json", "name": "Bad Name"}, ErrorCode.VALIDATION_FAILED),
    ],
)
async def test_bad_load_requests_fail_cleanly(tmp_path, api, params, code):
    (tmp_path / "broken.json").write_text("{nope", encoding="utf-8")
    result = await connector(tmp_path, api).execute("openapi_load", params)
    assert not result.ok and result.code is code


async def test_the_base_url_comes_from_the_environment_never_silently_from_the_spec(tmp_path, api):
    conn = connector(
        tmp_path, api
    )  # the spec names https://api.example.com but the environment is the local server
    await conn.execute("openapi_load", {"file": "openapi.json", "name": "aidly"})
    result = await authorized(
        conn,
        "openapi_call",
        {"spec": "aidly", "operation": "getUser", "path_params": ["id=1"], "environment": "dev"},
    )
    assert result.ok and api.requests[-1].target == "/users/1"


async def test_without_an_environment_the_specs_server_is_used_and_judged_like_any_url(tmp_path):
    conn = http_connector(tmp_path)
    (tmp_path / "openapi.json").write_text(json.dumps(spec()), encoding="utf-8")
    await conn.execute("openapi_load", {"file": "openapi.json", "name": "aidly"})
    params = {"spec": "aidly", "operation": "getUser", "path_params": ["id=1"]}
    assert (
        verdict(conn, "openapi_call", params, MANUAL) == "ask"
    )  # an untrusted remote origin: confirmed
    assert verdict(conn, "openapi_call", {**params, "operation": "deleteUser"}, AUTO) == "ask"


async def test_a_malicious_server_url_in_a_spec_cannot_reach_internal_addresses(tmp_path):
    document = spec(servers=[{"url": "http://169.254.169.254/latest"}])
    (tmp_path / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    conn = http_connector(tmp_path)
    await conn.execute("openapi_load", {"file": "openapi.json", "name": "evil"})
    params = {"spec": "evil", "operation": "getUser", "path_params": ["id=1"]}
    for mode in (MANUAL, AUTO, PLAN):
        assert verdict(conn, "openapi_call", params, mode) == "deny"
    assert not (await authorized(conn, "openapi_call", params)).ok


async def test_a_relative_server_url_needs_an_environment(tmp_path):
    (tmp_path / "openapi.json").write_text(
        json.dumps(spec(servers=[{"url": "/api"}])), encoding="utf-8"
    )
    conn = http_connector(tmp_path)
    await conn.execute("openapi_load", {"file": "openapi.json", "name": "rel"})
    result = await authorized(
        conn, "openapi_call", {"spec": "rel", "operation": "getUser", "path_params": ["id=1"]}
    )
    assert not result.ok and "names no usable server" in result.error


async def test_only_a_bounded_number_of_specs_stay_loaded(tmp_path, api):
    conn = connector(tmp_path, api)
    for i in range(12):
        assert (await conn.execute("openapi_load", {"file": "openapi.json", "name": f"s{i}"})).ok
    assert len(conn._specs) == 8 and "s11" in conn._specs and "s0" not in conn._specs


# ── Remote specifications obey the network policy ───────────────────────────


async def test_a_remote_specification_is_fetched_through_the_policy(tmp_path):
    server = await start_server(
        lambda s: json_reply(json.dumps(spec())) if s.path == "/openapi.json" else (404, {}, b"")
    )
    try:
        conn = http_connector(tmp_path, trusted=(server.base,))
        loaded = await conn.execute(
            "openapi_load", {"url": f"{server.base}/openapi.json", "name": "remote"}
        )
        assert loaded.ok and loaded.data["endpoints"] == 5
        missing = await conn.execute(
            "openapi_load", {"url": f"{server.base}/nope.json", "name": "x"}
        )
        assert not missing.ok and "HTTP 404" in missing.error
    finally:
        await server.stop()


async def test_a_remote_specification_from_an_untrusted_origin_is_confirmed_and_internal_ones_are_denied(
    tmp_path,
):
    conn = http_connector(tmp_path)
    assert (
        verdict(conn, "openapi_load", {"url": "https://example.com/openapi.json"}, MANUAL) == "ask"
    )
    assert verdict(conn, "openapi_load", {"url": "https://example.com/openapi.json"}, PLAN) == "ask"
    for url in ("http://169.254.169.254/spec", "http://192.168.0.5/spec"):
        assert verdict(conn, "openapi_load", {"url": url}, AUTO) == "deny"
    result = await conn.execute("openapi_load", {"url": "http://169.254.169.254/spec"})
    assert not result.ok and result.code is ErrorCode.NETWORK_POLICY


async def test_loading_a_local_spec_is_a_read_in_every_mode_but_outside_paths_are_flagged(
    tmp_path, api
):
    conn = connector(tmp_path, api)
    for mode in (MANUAL, AUTO, PLAN):
        assert verdict(conn, "openapi_load", {"file": "openapi.json"}, mode) == "allow"
    assert verdict(conn, "openapi_load", {"file": "../../etc/hosts"}, AUTO, tmp_path) == "ask"
    assert verdict(conn, "openapi_search", {"spec": "x"}, PLAN) == "allow"


async def test_an_oversized_remote_specification_is_refused(tmp_path):
    big = json.dumps(
        spec(
            paths={
                f"/p{i}": {"get": {"operationId": f"o{i}", "summary": "s" * 200, "responses": {}}}
                for i in range(40000)
            }
        )
    )
    server = await start_server(lambda s: json_reply(big))
    try:
        conn = http_connector(tmp_path, trusted=(server.base,))
        result = await conn.execute("openapi_load", {"url": f"{server.base}/big.json"})
        assert not result.ok and result.code in (
            ErrorCode.RESPONSE_TOO_LARGE,
            ErrorCode.VALIDATION_FAILED,
        )
    finally:
        await server.stop()
