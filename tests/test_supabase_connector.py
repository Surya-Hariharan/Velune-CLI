"""Supabase (REST): RLS by default, the service-role key never exposed (VAIR A5)."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.http_env import decide, verdict
from tests.integration_fakes import ScriptedProvider, call, final, make_env, turn
from velune.permissions.actions import ActionType, Risk
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode
from velune.resources import connections as connections_module
from velune.resources.base import ErrorCode
from velune.resources.capabilities import build_manifest
from velune.resources.connections import NamedConnections
from velune.resources.supabase.connector import SupabaseConnector

MANUAL, AUTO, PLAN = ExecutionMode.MANUAL, ExecutionMode.AUTO, ExecutionMode.PLAN
ANON, SERVICE = "anon-key-AAAA1111", "service-key-SSSS9999"
URL = "https://abc.supabase.co"


class Project:
    """A fake Supabase project: records requests, answers from a table of handlers."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rows: list[dict] = [{"id": 1, "email": "a@b.c", "password_hash": "x"}]
        self.status = 200
        self.error_body: dict | None = None
        self.raise_exc: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.status >= 400:
            return httpx.Response(self.status, json=self.error_body or {"message": "denied"})
        path = request.url.path
        if path == "/rest/v1/":
            return httpx.Response(
                200,
                json={"definitions": {"users": {"type": "object"}, "posts": {"type": "object"}}},
            )
        if path == "/storage/v1/bucket":
            return httpx.Response(200, json=[{"name": "avatars"}])
        return httpx.Response(200, json=self.rows)


@pytest.fixture
def env(monkeypatch):
    store: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(connections_module, "load_resource_secret", lambda t, n: store.get((t, n)))
    monkeypatch.setattr(
        NamedConnections,
        "_stored_names",
        lambda inst: sorted(n for (t, n) in store if t == inst.resource_id),
    )
    project = Project()
    store[("supabase", "supabase")] = {"url": URL, "anon_key": ANON, "service_role_key": SERVICE}

    def make(settings=None, **kw):
        c = SupabaseConnector(settings=settings or {}, workspace_trusted=True, **kw)

        def client(config, service_role=False):
            key = (config.get("service_role_key") if service_role else None) or config.get(
                "anon_key"
            )
            return httpx.AsyncClient(
                base_url=config.get("_base_url") or URL,
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                transport=httpx.MockTransport(project.handler),
            )

        c._client = client  # type: ignore[method-assign]
        return c

    class E:
        pass

    e = E()
    e.store, e.project, e.make = store, project, make
    return e


def v(conn, action, params, mode, tmp_path):
    return verdict(conn, action, params, mode, tmp_path)


# ── RLS applies by default ──────────────────────────────────────────────────


async def test_queries_use_the_anon_key_so_row_level_security_applies(env):
    c = env.make()
    result = await c.execute("query", {"table": "users", "limit": 5})
    assert result.ok and result.data["role"].startswith("anon")
    sent = env.project.requests[-1]
    assert sent.headers["apikey"] == ANON and sent.headers["authorization"] == f"Bearer {ANON}"


async def test_the_service_role_key_is_used_only_when_asked_for_and_is_labelled(env):
    c = env.make()
    result = await c.execute("query", {"table": "users", "service_role": True})
    assert result.ok and result.data["role"] == "service_role"
    assert env.project.requests[-1].headers["apikey"] == SERVICE


async def test_neither_key_ever_appears_in_a_result_or_a_listing(env):
    c = env.make()
    outputs = [
        await c.execute("list_connections", {}),
        await c.execute("verify", {}),
        await c.execute("list_tables", {}),
        await c.execute("query", {"table": "users"}),
        await c.execute("query", {"table": "users", "service_role": True}),
        await c.execute("storage_buckets", {"service_role": True}),
    ]
    text = json.dumps([o.to_dict() for o in outputs], default=str) + repr(c.status())
    assert ANON not in text and SERVICE not in text
    listing = outputs[0].data["connections"][0]
    assert listing["service_role_available"] is True  # a fact, never the key
    assert "service_role_key" not in listing


async def test_an_error_never_echoes_a_key(env):
    env.project.status = 401
    env.project.error_body = {"message": f"Invalid API key {ANON}"}
    result = await env.make().execute("query", {"table": "users"})
    assert not result.ok and ANON not in result.error


# ── Using the service role is a privileged act ──────────────────────────────


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("query", {"table": "users", "service_role": True}),
        ("storage_buckets", {"service_role": True}),
        ("rls_policies", {}),
    ],
)
def test_service_role_use_asks_in_every_mode_that_can_ask_and_is_denied_in_plan(
    env, tmp_path, action, params
):
    c = env.make()
    assert [v(c, action, params, m, tmp_path) for m in (MANUAL, PLAN, AUTO)] == [
        "ask",
        "deny",
        "ask",
    ]
    actions = decide(c, action, params, AUTO, tmp_path)[1]
    assert actions[0].scope == "supabase.service_role.use" and actions[0].secret
    assert actions[0].risk is Risk.HIGH and actions[0].action_type is ActionType.INTEGRATION_WRITE
    assert "bypasses Row Level Security" in actions[0].reason


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("query", {"table": "users"}),
        ("list_tables", {}),
        ("inspect_schema", {"table": "users"}),
        ("verify", {}),
        ("storage_buckets", {}),
        ("list_connections", {}),
    ],
)
def test_anon_reads_need_no_confirmation(env, tmp_path, action, params):
    assert [v(env.make(), action, params, m, tmp_path) for m in (MANUAL, PLAN, AUTO)] == [
        "allow"
    ] * 3


async def test_the_service_role_is_not_covered_by_a_session_grant(env, tmp_path):
    asked: list = []

    async def ask(tool, decision):
        asked.append(decision)
        return Approval.ALLOW_TASK

    e = make_env(tmp_path, AUTO, connectors={"supabase": lambda: env.make()}, ask=ask)
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="supabase")),
        turn(call("supabase__query", "q1", table="users", service_role=True)),
        turn(call("supabase__query", "q2", table="users", service_role=True)),
        final("done"),
    )
    await e.run(provider, "read users bypassing RLS")
    assert len(asked) == 2


async def test_without_a_stored_service_role_key_the_privileged_actions_fail_clearly(env):
    env.store[("supabase", "supabase")] = {"url": URL, "anon_key": ANON}
    c = env.make()
    result = await c.execute("rls_policies", {})
    assert not result.ok and "service-role" in result.error.lower()
    fallback = await c.execute("query", {"table": "users", "service_role": True})
    assert env.project.requests[-1].headers["apikey"] == ANON  # no key to escalate with
    assert fallback.ok


# ── Bounded, redacted, structured results ───────────────────────────────────


async def test_rows_are_capped_and_the_cap_cannot_be_raised(env):
    env.project.rows = [{"id": i} for i in range(1000)]
    c = env.make({"max_rows": 20})
    result = await c.execute("query", {"table": "users", "limit": 1000})
    assert env.project.requests[-1].url.params["limit"] == "20"
    assert result.data["rowcount"] == 20 and result.data["truncated"] is True
    assert "offset" in result.data["note"]


async def test_sensitive_columns_are_masked(env):
    result = await env.make().execute("query", {"table": "users"})
    assert result.data["rows"] == [{"id": 1, "email": "a@b.c", "password_hash": "***"}]
    assert result.data["redacted_columns"] == ["password_hash"]


async def test_filter_order_and_offset_are_sent_as_query_parameters(env):
    await env.make().execute(
        "query",
        {
            "table": "users",
            "select": "id,email",
            "order": "id",
            "descending": True,
            "offset": 10,
            "filter_column": "email",
            "filter_op": "ilike",
            "filter_value": "%@example.com",
        },
    )
    params = env.project.requests[-1].url.params
    assert params["order"] == "id.desc" and params["offset"] == "10"
    assert params["email"] == "ilike.%@example.com" and params["select"] == "id,email"


@pytest.mark.parametrize(
    "bad",
    [
        {"table": "../auth/users"},
        {"table": "users", "order": "id; drop"},
        {"table": "users", "filter_column": "a b", "filter_op": "eq", "filter_value": "1"},
        {"table": "users", "filter_column": "id", "filter_op": "in", "filter_value": "1"},
        {"table": "users", "filter_column": "id", "filter_op": "eq"},
        {"table": "users", "select": "*,secret_fn()::text; drop"},
    ],
)
async def test_hostile_arguments_never_reach_the_network(env, bad):
    result = await env.make().execute("query", bad)
    assert not result.ok and result.code is ErrorCode.VALIDATION_FAILED
    assert env.project.requests == []


# ── Errors are normalized ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ErrorCode.AUTHENTICATION_FAILED),
        (403, ErrorCode.AUTHORIZATION_DENIED),
        (404, ErrorCode.NOT_FOUND),
        (400, ErrorCode.VALIDATION_FAILED),
        (429, ErrorCode.UNAVAILABLE),
        (503, ErrorCode.UNAVAILABLE),
        (504, ErrorCode.TIMEOUT),
    ],
)
async def test_http_failures_map_to_codes_with_a_hint(env, status, code):
    env.project.status = status
    result = await env.make().execute("query", {"table": "users"})
    assert not result.ok and result.code is code and str(status) in result.error
    assert result.hint


async def test_a_403_explains_it_is_row_level_security_not_a_broken_connector(env):
    env.project.status = 403
    result = await env.make().execute("query", {"table": "users"})
    assert "Row Level Security" in result.hint


async def test_postgrest_details_that_could_echo_rows_are_not_surfaced(env):
    env.project.status = 400
    env.project.error_body = {"message": "bad", "details": "Key (email)=(a@b.c) exists"}
    result = await env.make().execute("query", {"table": "users"})
    assert "a@b.c" not in result.error


async def test_network_failures_are_structured(env):
    env.project.raise_exc = httpx.ConnectError("refused")
    down = await env.make().execute("query", {"table": "users"})
    assert not down.ok and down.code is ErrorCode.CONNECTION_FAILED
    env.project.raise_exc = httpx.ReadTimeout("slow")
    slow = await env.make().execute("query", {"table": "users"})
    assert slow.code is ErrorCode.TIMEOUT


# ── Named projects ──────────────────────────────────────────────────────────


async def test_several_projects_are_addressed_by_name_and_each_stays_on_its_own_host(env):
    env.store[("supabase", "staging")] = {
        "url": "https://stg.supabase.co",
        "anon_key": "stg-anon-key-1",
    }
    c = env.make()
    listing = await c.execute("list_connections", {})
    assert {x["name"] for x in listing.data["connections"]} == {"staging", "supabase"}
    await c.execute("query", {"table": "users", "connection": "staging"})
    assert env.project.requests[-1].url.host == "stg.supabase.co"
    assert env.project.requests[-1].headers["apikey"] == "stg-anon-key-1"
    await c.execute("query", {"table": "users", "connection": "supabase"})
    assert env.project.requests[-1].url.host == "abc.supabase.co"


async def test_an_ambiguous_project_is_an_error_not_a_guess(env):
    env.store[("supabase", "staging")] = {"url": "https://stg.supabase.co", "anon_key": "k"}
    env.store.pop(("supabase", "supabase"))
    env.store[("supabase", "prod")] = {"url": URL, "anon_key": "k2"}
    result = await env.make().execute("query", {"table": "users"})
    assert not result.ok and "connection" in result.error and env.project.requests == []


async def test_an_unknown_project_is_refused(env):
    result = await env.make().execute("query", {"table": "users", "connection": "nope"})
    assert not result.ok and result.code is ErrorCode.NOT_FOUND


async def test_only_an_https_project_url_is_ever_contacted(env):
    for url in (
        "http://abc.supabase.co",
        "ftp://x",
        "https://user:pw@abc.supabase.co",
        "not a url",
    ):
        env.store[("supabase", "supabase")] = {"url": url, "anon_key": "k"}
        result = await env.make().execute("query", {"table": "users"})
        assert not result.ok and env.project.requests == []


async def test_a_custom_port_is_kept_for_self_hosted_projects(env):
    assert (
        SupabaseConnector._validate_url("https://supa.internal:8443")
        == "https://supa.internal:8443"
    )


async def test_the_environment_label_is_shown(env):
    c = env.make({"connections": {"supabase": {"environment": "production"}}})
    listing = await c.execute("list_connections", {})
    assert listing.data["connections"][0]["environment"] == "production"


# ── Scope: SQL is the postgres integration's job ────────────────────────────


def test_supabase_declares_no_sql_or_write_operations(env):
    actions = {c.action for c in env.make().capabilities()}
    assert "execute" not in actions and not any("transaction" in a for a in actions)
    assert actions == {
        "list_connections",
        "verify",
        "list_tables",
        "inspect_schema",
        "rls_policies",
        "query",
        "storage_buckets",
        "edge_functions",
    }


def test_supabase_operations_are_grouped_for_discovery(env):
    manifest = build_manifest(env.make())
    cats = {op.action: op.category for op in manifest.operations}
    assert cats["query"] == "query_read" and cats["list_tables"] == "schema"
    assert cats["verify"] == "connections" and cats["storage_buckets"] == "storage"
    assert cats["edge_functions"] == "functions"


def test_the_model_facing_schema_describes_the_service_role_flag_as_privileged(env):
    manifest = build_manifest(env.make())
    query = manifest.operation("query")
    assert (
        "BYPASSES Row Level Security" in query.schema["properties"]["service_role"]["description"]
    )
    assert ANON not in json.dumps(query.schema) and SERVICE not in json.dumps(query.schema)
