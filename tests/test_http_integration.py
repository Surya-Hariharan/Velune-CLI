"""The HTTP integration through A2 discovery and the real tool loop (VAIR A3).

The scenario from the brief — "test the login API on my local backend" — driven end
to end by a scripted model: discover, enable, (next turn) call, inspect. The real
ToolLoopRunner, ResourceManager, PermissionGate and HTTP executor run; only the model
is scripted and the "backend" is a real local server.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.http_env import http_connector, write_collection
from tests.http_fakes import isolate_keystore
from tests.http_server import json_reply, start_server, text_reply
from tests.integration_fakes import ScriptedProvider, call, final, make_env, many_ops_app, turn
from velune.core.types.inference import ToolCall
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode

MANUAL, AUTO, PLAN = ExecutionMode.MANUAL, ExecutionMode.AUTO, ExecutionMode.PLAN
HTTP_TOOLS = [
    "http__request",
    "http__diagnose",
    "http__environments",
    "http__collection_list",
    "http__collection_show",
    "http__collection_run",
    "http__collection_import",
    "http__collection_export",
    "http__openapi_load",
    "http__openapi_search",
    "http__openapi_call",
]


@pytest.fixture(autouse=True)
def keystore(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path / "keys")


def payload(invocation: Any) -> dict[str, Any]:
    return json.loads(invocation.result)


@pytest.fixture
async def backend():
    def handler(seen):
        if seen.path == "/auth/login" and seen.method == "POST":
            body = json.loads(seen.body or b"{}")
            if body.get("password") == "correct-horse":
                return json_reply(
                    '{"data": {"token": "jwt-abc", "user": {"id": 1, "status": "active"}}}'
                )
            return text_reply('{"error": "invalid credentials"}', 401)
        return json_reply('{"ok": true}')

    server = await start_server(handler)
    yield server
    await server.stop()


def make(tmp_path, backend, mode=MANUAL, *, answer=Approval.ALLOW_ONCE, **kw):
    conn = http_connector(
        tmp_path, trusted=(backend.base,), environments={"local": {"base_url": backend.base}}, **kw
    )
    asked: list[Any] = []

    async def ask(tool, decision):
        asked.append(decision)
        return answer

    env = make_env(tmp_path, mode, connectors={"http": lambda: conn}, ask=ask)
    return env, asked


DISCOVERY = ["integrations_list", "integrations_enable", "integrations_disable"]


def tool_call(name: str, call_id: str, arguments: dict[str, Any]) -> ToolCall:
    """A tool call whose arguments may include a key named 'name'."""
    return ToolCall(id=call_id, name=name, arguments=arguments)


def enable_http(**kw):
    return call("integrations_enable", "e1", integration="http", **kw)


async def test_the_http_integration_is_discoverable_with_its_categories(tmp_path, backend):
    env, _ = make(tmp_path, backend)
    provider = ScriptedProvider(turn(call("integrations_list", "l1")), final())
    result = await env.run(provider, "test the login API on my local backend")
    rows = {r["id"]: r for r in payload(result.invocations[0])["integrations"]}
    row = rows["http"]
    assert row["state"] in ("available", "connected") and row["suggested"] is True
    categories = {c["name"]: c["operations"] for c in row["categories"]}
    assert set(categories) == {"requests", "environments", "collections", "openapi"}
    assert categories["requests"] == ["request", "diagnose"] and len(categories["collections"]) == 5
    assert row["operations"] == 11
    assert str(backend.port) not in json.dumps(
        result.invocations[0].result
    )  # no environment details leak


async def test_login_api_scenario_discover_enable_call_inspect(tmp_path, backend):
    env, asked = make(tmp_path, backend, MANUAL)
    provider = ScriptedProvider(
        turn(call("integrations_list", "l1")),
        turn(enable_http()),
        turn(
            call(
                "http__request",
                "r1",
                method="POST",
                url="/auth/login",
                json='{"email": "ada@example.com", "password": "correct-horse"}',
                expect=[
                    "status == 200",
                    "json data.token exists",
                    "json data.user.status == active",
                ],
            )
        ),
        final("The login API works: it returned a token for an active user."),
    )
    result = await env.run(provider, "test the login API on my local backend")
    assert provider.tool_names[0] == ["integrations_list", "integrations_enable"]
    assert provider.tool_names[2] == [*DISCOVERY, *HTTP_TOOLS]  # the next turn
    login = payload(result.invocations[2])
    assert login["ok"] is True and login["data"]["status"] == 200
    assert login["data"]["assertions"]["passed"] is True
    assert (
        login["data"]["json"]["data"]["token"] == "***REDACTED***"
    )  # the token is not given to the model
    assert len(asked) == 1  # the POST, and only the POST, needed approval in manual mode
    assert [r["decision"] for r in env.records if "http" in r["tool"]] == ["ask"]
    assert result.content.startswith("The login API works")


async def test_a_failing_login_is_a_normal_result_the_model_can_explain(tmp_path, backend):
    env, _ = make(tmp_path, backend, AUTO)
    provider = ScriptedProvider(
        turn(enable_http()),
        turn(
            call(
                "http__request",
                "r1",
                method="POST",
                url="/auth/login",
                json='{"password": "wrong"}',
                expect=["status == 200"],
            )
        ),
        final("Login is rejected."),
    )
    result = await env.run(provider, "check login")
    outcome = payload(result.invocations[1])
    assert outcome["ok"] is True and outcome["data"]["status"] == 401
    assert outcome["data"]["outcome"] == "assertion_failed"
    assert outcome["data"]["assertions"]["results"][0]["actual"] == 401
    assert not result.invocations[1].error  # a result, not a tool failure


async def test_a_refused_connection_does_not_make_the_integration_unavailable(tmp_path):
    server = await start_server(lambda s: json_reply("{}"))
    port = server.port
    await server.stop()
    conn = http_connector(tmp_path, trusted=(f"http://127.0.0.1:{port}",), connect_timeout=10)
    env = make_env(tmp_path, AUTO, connectors={"http": lambda: conn})
    provider = ScriptedProvider(
        turn(enable_http()),
        turn(call("http__request", "r1", method="GET", url=f"http://127.0.0.1:{port}/")),
        turn(call("integrations_list", "l1")),
        final("The API is down."),
    )
    result = await env.run(provider, "is the api up")
    failed = payload(result.invocations[1])
    assert (
        failed["ok"] is False
        and failed["error"]["code"] == "connection_failed"
        and "listening" in failed["error"]["hint"]
    )
    rows = {r["id"]: r for r in payload(result.invocations[2])["integrations"]}
    assert (
        rows["http"]["state"] != "unavailable"
    )  # the HTTP client works; the API it called did not


async def test_only_the_requested_category_is_exposed_and_hidden_tools_cannot_be_called(
    tmp_path, backend
):
    env, _ = make(tmp_path, backend, AUTO)
    provider = ScriptedProvider(
        turn(enable_http(categories=["requests"])),
        turn(tool_call("http__collection_run", "c1", {"name": "suite"})),
        final("done"),
    )
    result = await env.run(provider, "do it")
    assert provider.tool_names[1] == [*DISCOVERY, "http__request", "http__diagnose"]
    assert (
        result.invocations[1].error and "http__collection_run" in result.invocations[1].result
    )  # unknown tool: refused
    assert env.source.scope.enabled_ids() == ["http"]


async def test_discovery_never_grants_authorization_in_plan_mode(tmp_path, backend):
    env, asked = make(tmp_path, backend, PLAN)
    provider = ScriptedProvider(
        turn(enable_http()),
        turn(call("http__request", "r1", method="POST", url="/auth/login", json="{}")),
        turn(call("http__request", "r2", method="GET", url="/ok")),
        final("planned"),
    )
    result = await env.run(provider, "try it")
    post, get = result.invocations[1], result.invocations[2]
    assert post.error and "plan mode" in post.result and asked == []
    assert payload(get)["ok"] is True  # reads still work while planning
    assert [r.method for r in backend.requests] == ["GET"]


async def test_the_dispatcher_cannot_bypass_the_http_policy(tmp_path, backend):
    """With many integrations the budget moves operations behind integration_call; same rules apply."""
    conn = http_connector(
        tmp_path, trusted=(backend.base,), environments={"local": {"base_url": backend.base}}
    )
    env = make_env(
        tmp_path, PLAN, connectors={"bigapp": many_ops_app(30), "http": lambda: conn}, max_tools=6
    )
    provider = ScriptedProvider(
        turn(enable_http()),
        turn(
            call(
                "integration_call",
                "d1",
                integration="http",
                operation="request",
                arguments=json.dumps({"method": "DELETE", "url": "/x"}),
            )
        ),
        final("done"),
    )
    result = await env.run(provider, "delete it")
    assert result.invocations[1].error and backend.requests == []


async def test_a_denied_prompt_is_reported_and_nothing_is_sent(tmp_path, backend):
    env, asked = make(tmp_path, backend, MANUAL, answer=Approval.DENY)
    provider = ScriptedProvider(
        turn(enable_http()),
        turn(call("http__request", "r1", method="DELETE", url="/users/1")),
        final("could not delete"),
    )
    result = await env.run(provider, "delete user 1")
    assert result.invocations[1].error and len(asked) == 1 and backend.requests == []


async def test_collections_run_through_the_loop_with_one_approval(tmp_path, backend):
    write_collection(
        tmp_path,
        {
            "name": "smoke",
            "environment": "local",
            "requests": [
                {
                    "id": "ok",
                    "method": "GET",
                    "url": "{{base_url}}/ok",
                    "assertions": ["status == 200"],
                },
                {
                    "id": "login",
                    "method": "POST",
                    "url": "{{base_url}}/auth/login",
                    "body": {"json": {"password": "wrong"}},
                    "assertions": ["status == 200"],
                },
                {"id": "gone", "method": "DELETE", "url": "{{base_url}}/users/9"},
            ],
        },
    )
    env, asked = make(tmp_path, backend, MANUAL)
    provider = ScriptedProvider(
        turn(enable_http()),
        turn(tool_call("http__collection_run", "c1", {"name": "smoke"})),
        final("2 of 3 passed; login failed."),
    )
    result = await env.run(provider, "run the smoke collection")
    summary = payload(result.invocations[1])["data"]
    assert (summary["total"], summary["passed"], summary["failed"]) == (3, 2, 1)
    assert [f["id"] for f in summary["failed_requests"]] == ["login"]
    assert len(asked) == 1  # one approval for the whole collection (it contains a DELETE)
    # The gate asks about the changes it could not allow on its own (the read was fine).
    assert {a.scope for a in asked[0].actions} == {"http.request.write", "http.request.delete"}


def test_the_default_manager_registers_the_http_integration(tmp_path):
    from velune.resources.manager import build_default_manager

    manager = build_default_manager(
        config={"http": {"environments": {"dev": {"base_url": "http://localhost:5000"}}}},
        workspace=tmp_path,
    )
    assert "http" in manager.list_ids()
    connector = manager.get("http")
    assert connector is not None and "dev" in connector.environments
    assert manager.get("http") is connector


def test_the_disabled_flag_removes_the_http_integration(tmp_path):
    from velune.resources.manager import build_default_manager

    manager = build_default_manager(config={"http": {"enabled": False}}, workspace=tmp_path)
    assert "http" not in manager.list_ids() and manager.get("http") is None
