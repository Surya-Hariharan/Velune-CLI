"""Dynamic tool exposure through the real tool loop, gate and manager (VAIR A2).

A scripted provider drives the production ToolLoopRunner. The tests assert on the
exact tool list of every request, on what actually ran, and on the permission
gate's audit records — discovery must never be mistaken for authorization.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.integration_fakes import (
    SECRET,
    EchoTool,
    FakeApp,
    OtherApp,
    ScriptedProvider,
    allow_all,
    call,
    final,
    make_env,
    many_ops_app,
    turn,
)
from velune.core.redaction import register_secret_value, unregister_secret_value
from velune.orchestration.tool_loop import approve_readonly_only
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode
from velune.resources.base import (
    ErrorCode,
    ResourceCapability,
    ResourcePermission,
    ResourceResult,
)
from velune.tools.base.registry import ToolRegistry

DISCOVERY = ["integrations_list", "integrations_enable"]
FAKE_OPS = ["fakeapp__peek", "fakeapp__count", "fakeapp__poke", "fakeapp__nuke"]


def payload(invocation: Any) -> dict[str, Any]:
    return json.loads(invocation.result)


def native_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


async def ask_once(tool: str, decision: Any) -> Approval:
    return Approval.ALLOW_ONCE


def enable(integration: str = "fakeapp", **kw: Any):
    return call("integrations_enable", "e1", integration=integration, **kw)


# ── Initial exposure ────────────────────────────────────────────────────────


async def test_only_discovery_tools_are_offered_at_first(tmp_path):
    env = make_env(tmp_path, connectors={"fakeapp": FakeApp, "otherapp": OtherApp})
    provider = ScriptedProvider(final())
    await env.run(provider, registry=native_registry())
    assert provider.tool_names[0] == ["native_echo", *DISCOVERY]


async def test_discovery_tool_descriptions_carry_no_environment_details(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(final())
    await env.run(provider, "use the fake widget")
    blob = json.dumps(provider.requests[0].tools)
    assert "fakeapp" in blob and str(tmp_path) not in blob
    assert "Suggested for this task: fakeapp" in blob  # deterministic suggestion


# ── Discover → enable → use ─────────────────────────────────────────────────


async def test_enable_then_call_across_turns(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(
        turn(enable()),
        turn(call("fakeapp__peek", "p1", key="a")),
        final("all done"),
    )
    result = await env.run(provider)

    names = provider.tool_names
    assert not any(n.startswith("fakeapp__") for n in names[0])
    assert set(FAKE_OPS) <= set(names[1]) and "integrations_disable" in names[1]
    assert names[1] == names[2]  # persists on later turns

    enabled, peeked = result.invocations
    assert enabled.source == "integration" and not enabled.error
    body = payload(enabled)
    assert body["enabled"] and body["available_from"] == "your next turn"
    assert {t["name"] for t in body["tools"]} == set(FAKE_OPS)
    assert {t["name"]: t["access"] for t in body["tools"]}["fakeapp__nuke"] == "destructive"

    assert not peeked.error and peeked.source == "integration"
    data = payload(peeked)
    assert data["ok"] and data["integration"] == "fakeapp" and data["data"]["key"] == "a"
    assert data["data"]["password"] == "***REDACTED***"  # sensitive key; see the secrets test
    assert env.manager.get("fakeapp").ran == [("peek", {"key": "a"})]
    assert result.content == "all done"


async def test_two_model_round_trips_before_the_first_operation(tmp_path):
    """One discovery call is enough: the enable tool already names the integrations."""
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable()), turn(call("fakeapp__count", "c1")), final())
    result = await env.run(provider)
    discovery_calls = [i for i in result.invocations if i.call.name.startswith("integrations_")]
    assert len(discovery_calls) == 1 and len(provider.requests) == 3


async def test_the_audit_trail_records_the_real_tool_and_scope(tmp_path):
    env = make_env(tmp_path)
    await env.run(ScriptedProvider(turn(enable()), turn(call("fakeapp__peek", key="a")), final()))
    last = env.records[-1]
    assert last["tool"] == "fakeapp__peek" and last["outcome"] == "allowed"
    assert last["actions"][0]["scope"] == "fakeapp.item.read"
    assert last["actions"][0]["type"] == "INTEGRATION_READ"


async def test_enabled_tools_persist_across_runs_in_a_session(tmp_path):
    env = make_env(tmp_path)
    await env.run(ScriptedProvider(turn(enable()), final()))
    second = ScriptedProvider(final())
    await env.run(second, "a follow-up question")
    assert set(FAKE_OPS) <= set(second.tool_names[0])


async def test_irrelevant_integrations_stay_hidden(tmp_path):
    env = make_env(tmp_path, connectors={"fakeapp": FakeApp, "otherapp": OtherApp})
    provider = ScriptedProvider(turn(enable()), turn(call("fakeapp__count")), final())
    await env.run(provider)
    assert not any(n.startswith("otherapp__") for names in provider.tool_names for n in names)


async def test_repeated_enable_does_not_duplicate_tools_or_rebuild(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable()), turn(enable()), final())
    result = await env.run(provider)
    assert payload(result.invocations[1])["already_enabled"] is True
    for names in provider.tool_names:
        assert len(names) == len(set(names))
    assert provider.tool_names[1] == provider.tool_names[2]
    changes = [e for e, _ in env.events if e == "tools_changed"]
    assert len(changes) == 1  # rebuilt once, not on every turn


async def test_categories_limit_what_is_exposed(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable(categories=["inspect"])), final())
    await env.run(provider)
    exposed = {n for n in provider.tool_names[1] if n.startswith("fakeapp__")}
    assert exposed == {"fakeapp__peek", "fakeapp__count"}


async def test_disable_removes_the_tools_on_the_next_turn(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(
        turn(enable()), turn(call("integrations_disable", "d1", integration="fakeapp")), final()
    )
    result = await env.run(provider)
    assert payload(result.invocations[1])["disabled"] is True
    assert any(n.startswith("fakeapp__") for n in provider.tool_names[1])
    assert not any(n.startswith("fakeapp__") for n in provider.tool_names[2])


async def test_list_reports_state_and_enabled_flag(tmp_path):
    env = make_env(tmp_path, connectors={"fakeapp": FakeApp, "otherapp": OtherApp})
    provider = ScriptedProvider(turn(enable()), turn(call("integrations_list", "l1")), final())
    result = await env.run(provider, "read a widget")
    rows = {r["id"]: r for r in payload(result.invocations[1])["integrations"]}
    assert rows["fakeapp"]["enabled"] and rows["fakeapp"]["suggested"]
    assert not rows["otherapp"]["enabled"] and rows["otherapp"]["state"] == "available"


# ── Hidden / unknown / unavailable fail closed ──────────────────────────────


async def test_a_hand_built_call_to_a_hidden_tool_is_refused(tmp_path):
    env = make_env(tmp_path, ExecutionMode.AUTO)
    provider = ScriptedProvider(turn(call("fakeapp__poke", key="a", value="b")), final())
    result = await env.run(provider)
    inv = result.invocations[0]
    assert inv.error and "unknown tool" in inv.result and "integrations_enable" in inv.result
    assert env.manager.get("fakeapp").ran == []


async def test_a_call_in_the_same_batch_as_enable_is_refused_with_guidance(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(
        turn(enable(), call("fakeapp__peek", "p1", key="a")),
        turn(call("fakeapp__peek", "p2", key="a")),
        final(),
    )
    result = await env.run(provider)
    early, _, later = result.invocations[0], result.invocations[1], result.invocations[2]
    assert early.error is False  # the enable itself succeeded
    refused = result.invocations[1]
    assert refused.error and "not enabled yet" in refused.result
    assert not later.error and env.manager.get("fakeapp").ran == [("peek", {"key": "a"})]


async def test_unknown_integration_fails_closed(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable("ghost")), final())
    result = await env.run(provider)
    body = payload(result.invocations[0])
    assert body["enabled"] is False and body["error"]["code"] == "not_found"
    assert "fakeapp" in body["error"]["message"]
    assert provider.tool_names[1] == provider.tool_names[0]  # nothing was added


@pytest.mark.parametrize(
    ("kwargs", "state"),
    [
        ({"available": False}, "unavailable"),
        ({"problem": "Not configured — run /resource configure fakeapp"}, "needs_configuration"),
    ],
)
async def test_unusable_integrations_are_not_enabled(tmp_path, kwargs, state):
    env = make_env(tmp_path, connectors={"fakeapp": lambda: FakeApp(**kwargs)})
    provider = ScriptedProvider(turn(enable()), final())
    result = await env.run(provider)
    body = payload(result.invocations[0])
    assert body["enabled"] is False and body["state"] == state and body["reason"]
    assert provider.tool_names[1] == provider.tool_names[0]


async def test_enable_rejects_bad_arguments_with_a_message(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(call("integrations_enable", "e1")), final())
    result = await env.run(provider)
    assert (
        result.invocations[0].error and "missing required argument" in result.invocations[0].result
    )


async def test_unknown_categories_are_reported(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable(categories=["inspect", "nope"])), final())
    result = await env.run(provider)
    assert payload(result.invocations[0])["unknown_categories"] == ["nope"]


async def test_invalid_arguments_to_an_operation_never_reach_the_connector(tmp_path):
    env = make_env(tmp_path, ExecutionMode.AUTO)
    provider = ScriptedProvider(
        turn(enable()),
        turn(
            call("fakeapp__peek", "a", key=5),
            call("fakeapp__peek", "b"),
            call("fakeapp__peek", "c", key="x", extra=1),
        ),
        final(),
    )
    result = await env.run(provider)
    assert all(i.error for i in result.invocations[1:])
    assert env.manager.get("fakeapp").ran == []


# ── Discovery is not authorization ──────────────────────────────────────────


@pytest.mark.parametrize("mode", list(ExecutionMode))
async def test_discovery_tools_work_in_every_mode(tmp_path, mode):
    env = make_env(tmp_path, mode)
    provider = ScriptedProvider(turn(enable()), turn(call("integrations_list", "l1")), final())
    result = await env.run(provider)
    assert not any(i.error for i in result.invocations)
    assert all(r["actions"][0]["type"] == "READ" for r in env.records)


async def test_plan_mode_exposes_tools_but_refuses_changes(tmp_path):
    env = make_env(tmp_path, ExecutionMode.PLAN)
    provider = ScriptedProvider(
        turn(enable()),
        turn(call("fakeapp__peek", "r", key="a"), call("fakeapp__poke", "w", key="a", value="b")),
        final(),
    )
    result = await env.run(provider)
    assert "fakeapp__poke" in provider.tool_names[1]  # visible…
    read, write = result.invocations[1], result.invocations[2]
    assert not read.error and write.error  # …but not permitted
    assert "plan mode" in write.result
    assert env.manager.get("fakeapp").ran == [("peek", {"key": "a"})]


async def test_manual_mode_without_a_person_refuses_changes(tmp_path):
    env = make_env(tmp_path, ExecutionMode.MANUAL)
    provider = ScriptedProvider(
        turn(enable()), turn(call("fakeapp__poke", key="a", value="b")), final()
    )
    result = await env.run(provider)
    assert result.invocations[1].error and "no one can be asked" in result.invocations[1].result
    assert env.manager.get("fakeapp").ran == []


async def test_manual_mode_runs_a_change_once_the_user_approves(tmp_path):
    env = make_env(tmp_path, ExecutionMode.MANUAL, ask=ask_once)
    provider = ScriptedProvider(
        turn(enable()), turn(call("fakeapp__poke", key="a", value="b")), final()
    )
    result = await env.run(provider)
    assert not result.invocations[1].error
    assert env.manager.get("fakeapp").ran == [("poke", {"key": "a", "value": "b"})]
    assert env.records[-1]["outcome"] == "approved by user (allow_once)"


async def test_auto_mode_runs_ordinary_changes_but_still_asks_for_destructive_ones(tmp_path):
    env = make_env(tmp_path, ExecutionMode.AUTO)
    provider = ScriptedProvider(
        turn(enable()),
        turn(call("fakeapp__poke", "w", key="a", value="b"), call("fakeapp__nuke", "n")),
        final(),
    )
    result = await env.run(provider)
    poke, nuke = result.invocations[1], result.invocations[2]
    assert not poke.error and nuke.error
    assert [a for a, _ in env.manager.get("fakeapp").ran] == ["poke"]
    assert env.records[-1]["actions"][0]["type"] == "INTEGRATION_DELETE"


@pytest.mark.parametrize("mode", [ExecutionMode.PLAN, ExecutionMode.MANUAL, ExecutionMode.AUTO])
@pytest.mark.parametrize(
    ("action", "args"),
    [("peek", {"key": "a"}), ("count", {}), ("poke", {"key": "a", "value": "b"}), ("nuke", {})],
)
async def test_a_discovered_tool_is_decided_exactly_like_a_direct_call(
    tmp_path, mode, action, args
):
    """Exposure through the model changes no verdict: same policy as manager.execute."""
    direct_env = make_env(tmp_path / "direct", mode)
    (tmp_path / "direct").mkdir(exist_ok=True)
    direct = await direct_env.manager.execute("fakeapp", action, args)

    (tmp_path / "model").mkdir(exist_ok=True)
    env = make_env(tmp_path / "model", mode)
    provider = ScriptedProvider(turn(enable()), turn(call(f"fakeapp__{action}", **args)), final())
    result = await env.run(provider)
    assert result.invocations[1].error == (not direct.ok)
    assert [a for a, _ in env.manager.get("fakeapp").ran] == ([action] if direct.ok else [])


async def test_the_default_approver_still_denies_changes_but_allows_reads(tmp_path):
    env = make_env(tmp_path, ExecutionMode.AUTO)
    provider = ScriptedProvider(
        turn(enable()),
        turn(call("fakeapp__peek", "r", key="a"), call("fakeapp__poke", "w", key="a", value="b")),
        final(),
    )
    result = await env.run(provider, approver=approve_readonly_only)
    assert not result.invocations[0].error and not result.invocations[1].error
    assert result.invocations[2].error and "denied permission" in result.invocations[2].result
    assert [a for a, _ in env.manager.get("fakeapp").ran] == ["peek"]


async def test_a_connector_redefined_after_enabling_is_refused(tmp_path):
    """Scope mismatch: the exposed scope no longer matches the connector's — fail closed."""
    env = make_env(tmp_path, ExecutionMode.AUTO)
    await env.source.enable("fakeapp")

    class Escalated(FakeApp):
        def capabilities(self):
            return [
                ResourceCapability(
                    "peek", ResourcePermission.READ, "r", scope="fakeapp.other.scope"
                ),
                *[c for c in super().capabilities() if c.action != "peek"],
            ]

    env.manager._connectors["fakeapp"] = Escalated()
    provider = ScriptedProvider(turn(call("fakeapp__peek", key="a")), final())
    env.source.begin_run = lambda *a, **k: None  # no refresh: exercise the call-time check
    result = await env.run(provider)
    assert result.invocations[0].error
    assert env.manager.get("fakeapp").ran == []


# ── Budget and the dispatcher ───────────────────────────────────────────────


def mixed_app():
    """Six read operations and one write, so overflow includes a change."""

    class Mixed(FakeApp):
        resource_id = "mixed"
        display_name = "Mixed App"
        keywords = ("mixed",)

        def capabilities(self):
            reads = [
                ResourceCapability(
                    f"r{i:02d}", ResourcePermission.READ, f"Read {i}", scope=f"mixed.item.r{i:02d}"
                )
                for i in range(6)
            ]
            write = ResourceCapability(
                "w00", ResourcePermission.WRITE, "Write", scope="mixed.item.w00"
            )
            return [*reads, write]

    return Mixed


async def test_overflow_is_reachable_through_the_dispatcher(tmp_path):
    env = make_env(tmp_path, connectors={"bigapp": many_ops_app(10)}, max_tools=4)
    provider = ScriptedProvider(
        turn(enable("bigapp")),
        turn(
            call("integration_call", "d1", integration="bigapp", operation="op09", arguments="{}")
        ),
        final(),
    )
    result = await env.run(provider)
    body = payload(result.invocations[0])
    assert len(body["tools"]) == 3 and len(body["via_integration_call"]) == 7
    direct = [n for n in provider.tool_names[1] if n.startswith("bigapp__")]
    assert len(direct) == 3 and "integration_call" in provider.tool_names[1]
    assert len(direct) + 1 <= 4  # the budget includes the dispatcher
    assert not result.invocations[1].error and payload(result.invocations[1])["ok"]
    assert env.manager.get("bigapp").ran == [("op09", {})]


async def test_the_dispatcher_is_authorized_as_the_operation_it_stands_for(tmp_path):
    env = make_env(tmp_path, ExecutionMode.AUTO, connectors={"mixed": mixed_app()}, max_tools=4)
    provider = ScriptedProvider(
        turn(enable("mixed")),
        turn(call("integration_call", integration="mixed", operation="r05")),
        final(),
    )
    await env.run(provider)
    last = env.records[-1]
    assert last["tool"] == "mixed__r05"  # not "integration_call"
    assert last["actions"][0]["scope"] == "mixed.item.r05"


async def test_the_dispatcher_cannot_run_a_change_in_plan_mode(tmp_path):
    env = make_env(tmp_path, ExecutionMode.PLAN, connectors={"mixed": mixed_app()}, max_tools=4)
    provider = ScriptedProvider(
        turn(enable("mixed")),
        turn(call("integration_call", integration="mixed", operation="w00")),
        final(),
    )
    result = await env.run(provider)
    assert result.invocations[1].error and "plan mode" in result.invocations[1].result
    assert env.manager.get("mixed").ran == []


async def test_a_session_grant_through_the_dispatcher_is_precise(tmp_path):
    asked: list[str] = []

    async def ask(tool, decision):
        asked.append(tool)
        return Approval.ALLOW_TASK

    env = make_env(
        tmp_path, ExecutionMode.MANUAL, ask=ask, connectors={"mixed": mixed_app()}, max_tools=4
    )
    provider = ScriptedProvider(
        turn(enable("mixed")),
        turn(call("integration_call", integration="mixed", operation="w00")),
        final(),
    )
    await env.run(provider)
    assert asked == ["mixed__w00"] and env.manager.get("mixed").ran == [("w00", {})]
    # The grant covers that one operation — not the dispatcher, not other operations.
    assert env.gate.session_grants == {"mixed__w00"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"integration": "mixed", "operation": "nope"},
        {"integration": "ghost", "operation": "r00"},
        {"integration": "mixed", "operation": "w00", "arguments": "not json"},
        {"integration": "mixed", "operation": "w00", "arguments": "[1, 2]"},
        {"integration": 5, "operation": "w00"},
    ],
)
async def test_the_dispatcher_refuses_anything_not_enabled_or_malformed(tmp_path, arguments):
    env = make_env(tmp_path, ExecutionMode.AUTO, connectors={"mixed": mixed_app()}, max_tools=4)
    provider = ScriptedProvider(
        turn(enable("mixed")), turn(call("integration_call", **arguments)), final()
    )
    result = await env.run(provider)
    assert result.invocations[1].error and env.manager.get("mixed").ran == []


async def test_there_is_no_dispatcher_when_everything_fits(tmp_path):
    env = make_env(tmp_path, ExecutionMode.AUTO)
    provider = ScriptedProvider(
        turn(enable()),
        turn(call("integration_call", integration="fakeapp", operation="peek")),
        final(),
    )
    result = await env.run(provider)
    assert "integration_call" not in provider.tool_names[1]
    assert result.invocations[1].error and "unknown tool" in result.invocations[1].result


async def test_dispatcher_arguments_may_be_an_object_or_json_text(tmp_path):
    env = make_env(tmp_path, ExecutionMode.AUTO, connectors={"fakeapp": FakeApp}, max_tools=4)
    # 4 ops fit exactly → force overflow with a 5th integration instead.
    env = make_env(
        tmp_path,
        ExecutionMode.AUTO,
        connectors={"fakeapp": FakeApp, "bigapp": many_ops_app(3)},
        max_tools=4,
    )
    provider = ScriptedProvider(
        turn(enable("fakeapp"), call("integrations_list", "l1")),
        turn(enable("bigapp")),
        turn(
            call(
                "integration_call",
                "d1",
                integration="fakeapp",
                operation="peek",
                arguments={"key": "x"},
            ),
            call(
                "integration_call",
                "d2",
                integration="fakeapp",
                operation="peek",
                arguments='{"key": "y"}',
            ),
        ),
        final(),
    )
    await env.run(provider)
    ran = [p for a, p in env.manager.get("fakeapp").ran if a == "peek"]
    assert ran == [{"key": "x"}, {"key": "y"}]


# ── MCP coexists and stays gated ────────────────────────────────────────────


class FakeMCP:
    def __init__(self):
        from velune.mcp.transports.base import ToolInfo

        self._tools = [
            ToolInfo("lookup", "d", {"type": "object", "properties": {}}, server_name="srv")
        ]
        self.calls: list[str] = []

    def all_tools(self):
        return list(self._tools)

    async def call_tool(self, qualified, arguments):
        self.calls.append(qualified)
        return "remote ok"


async def test_mcp_tools_coexist_and_remain_gated(tmp_path):
    env = make_env(tmp_path, ExecutionMode.PLAN)
    mcp = FakeMCP()
    provider = ScriptedProvider(
        turn(enable()),
        turn(call("srv_lookup", "m1"), call("fakeapp__peek", "p1", key="a")),
        final(),
    )
    result = await env.run(provider, mcp=mcp)
    assert {"srv_lookup", "fakeapp__peek"} <= set(provider.tool_names[1])
    assert result.invocations[1].source == "mcp" and result.invocations[1].error
    assert mcp.calls == []  # PLAN refuses MCP calls exactly as before
    assert not result.invocations[2].error  # a read integration call is fine in PLAN


async def test_an_integration_tool_cannot_shadow_a_native_tool(tmp_path):
    class Shadow(EchoTool):
        def get_name(self):
            return "integrations_list"

    registry = ToolRegistry()
    registry.register(Shadow())
    env = make_env(tmp_path)
    provider = ScriptedProvider(final())
    await env.run(provider, registry=registry)
    assert provider.tool_names[0].count("integrations_list") == 1


# ── Results, redaction and failures ─────────────────────────────────────────


async def test_secrets_never_reach_the_model(tmp_path):
    """A registered credential is masked in results, and absent from every schema and listing."""
    register_secret_value(SECRET)
    try:
        env = make_env(tmp_path)
        provider = ScriptedProvider(
            turn(enable()),
            turn(call("fakeapp__peek", "p1", key="a"), call("integrations_list", "l1")),
            final(),
        )
        result = await env.run(provider)
        peeked = payload(result.invocations[1])["data"]
        assert peeked["detail"] == "authenticated with ***REDACTED***"  # free text, by value
        assert peeked["password"] == "***REDACTED***"  # sensitive key
        everything = json.dumps([r.messages for r in provider.requests], default=str)
        everything += json.dumps([r.tools for r in provider.requests])
        assert SECRET not in everything
        assert SECRET not in "".join(i.result for i in result.invocations)
    finally:
        unregister_secret_value(SECRET)


async def test_a_result_is_valid_json_with_the_normalized_shape(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable()), turn(call("fakeapp__count")), final())
    result = await env.run(provider)
    body = payload(result.invocations[1])
    assert set(body) >= {"ok", "integration", "action", "data", "duration_ms"}


async def test_an_oversized_result_is_compacted_into_valid_json(tmp_path):
    class Chatty(FakeApp):
        async def execute(self, action, params=None):
            rows = [{"row": i, "pad": "x" * 200} for i in range(400)]
            return ResourceResult.success(action, rows)

    env = make_env(tmp_path, connectors={"fakeapp": Chatty})
    provider = ScriptedProvider(turn(enable()), turn(call("fakeapp__count")), final())
    result = await env.run(provider)
    body = payload(result.invocations[1])  # parses: not cut off mid-JSON
    assert body["data"]["truncated"] and body["data"]["total_items"] == 400
    assert body["data"]["items"][0]["row"] == 0 and body["warnings"]
    assert len(result.invocations[1].result) < 13_000


async def test_the_application_failing_mid_run_is_a_structured_error(tmp_path):
    env = make_env(
        tmp_path, connectors={"fakeapp": lambda: FakeApp(fail_with=ErrorCode.CONNECTION_FAILED)}
    )
    provider = ScriptedProvider(
        turn(enable()),
        turn(call("fakeapp__peek", "p1", key="a")),
        turn(call("integrations_list", "l1")),
        final("recovered"),
    )
    result = await env.run(provider)
    failed = payload(result.invocations[1])
    assert failed["ok"] is False and failed["error"]["code"] == "connection_failed"
    assert not result.invocations[1].error  # a normal tool result the model can act on
    rows = {r["id"]: r for r in payload(result.invocations[2])["integrations"]}
    assert rows["fakeapp"]["state"] == "unavailable"  # reflected without waiting for the TTL
    assert result.content == "recovered"


async def test_an_application_that_disappears_before_the_call(tmp_path):
    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable()), turn(call("fakeapp__peek", key="a")), final())
    original = env.source.enable

    async def enable_then_stop(*a, **k):
        out = await original(*a, **k)
        env.manager.get("fakeapp").available = False  # goes down right after enabling
        return out

    env.source.enable = enable_then_stop
    result = await env.run(provider)
    body = payload(result.invocations[1])
    assert body["ok"] is False and body["error"]["code"] == "connection_failed"
    assert env.manager.get("fakeapp").ran == []  # never reached the application


async def test_a_broken_dynamic_source_does_not_break_the_turn(tmp_path):
    class Broken:
        version = 0

        def tools(self):
            raise RuntimeError("boom")

        def unknown_tool_hint(self, name):
            raise RuntimeError("boom")

    from tests.integration_fakes import ScriptedProvider as P
    from velune.core.types.inference import InferenceRequest
    from velune.orchestration.tool_loop import ToolLoopRunner

    provider = P(turn(call("ghost")), final("fine"))
    runner = ToolLoopRunner(provider, native_registry(), dynamic_tools=Broken())
    result = await runner.run(
        InferenceRequest(model_id="m", messages=[{"role": "user", "content": "x"}])
    )
    assert result.content == "fine" and "unknown tool" in result.invocations[0].result
    assert provider.tool_names[0] == ["native_echo"]


async def test_without_a_dynamic_source_the_loop_is_unchanged(tmp_path):
    from velune.core.types.inference import InferenceRequest
    from velune.orchestration.tool_loop import ToolLoopRunner

    provider = ScriptedProvider(final())
    runner = ToolLoopRunner(provider, native_registry())
    await runner.run(InferenceRequest(model_id="m", messages=[{"role": "user", "content": "x"}]))
    assert provider.tool_names[0] == ["native_echo"]


async def test_explicit_request_tools_are_respected(tmp_path):
    """A caller that supplies its own tool list keeps it: nothing is rebuilt under it."""
    from velune.core.types.inference import InferenceRequest

    env = make_env(tmp_path)
    provider = ScriptedProvider(turn(enable()), final())
    env.source.begin_run("x")
    request = InferenceRequest(
        model_id="m",
        messages=[{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "integrations_enable", "parameters": {}}}],
    )
    await env.runner(provider).run(request)
    assert provider.tool_names[0] == provider.tool_names[1] == ["integrations_enable"]


# ── The REPL wiring ─────────────────────────────────────────────────────────


def fake_repl(manager, **resources):
    cfg = SimpleNamespace(resources=SimpleNamespace(**resources)) if resources else None
    return SimpleNamespace(_resource_manager=manager, runtime=SimpleNamespace(config=cfg))


def chat_request(text="use the fake widget"):
    from velune.core.types.inference import InferenceRequest

    return InferenceRequest(model_id="m", messages=[{"role": "user", "content": text}])


def test_the_chat_handler_builds_one_session_source(tmp_path):
    from velune.cli.handlers.tool_chat import _integration_source

    env = make_env(tmp_path)
    repl = fake_repl(env.manager, expose_to_model=True, max_exposed_tools=9)
    first = _integration_source(repl, chat_request(), tmp_path)
    second = _integration_source(repl, chat_request("another question"), tmp_path)
    assert first is second is repl._integration_tools
    assert first.scope.max_tools == 9
    assert first.suggested_ids() == []  # primed with the *latest* task text


def test_the_chat_handler_primes_suggestions_from_the_task(tmp_path):
    from velune.cli.handlers.tool_chat import _integration_source

    env = make_env(tmp_path)
    source = _integration_source(fake_repl(env.manager), chat_request("read the widget"), tmp_path)
    assert source.suggested_ids() == ["fakeapp"]


def test_the_chat_handler_offers_nothing_without_a_manager_or_when_disabled(tmp_path):
    from velune.cli.handlers.tool_chat import _integration_source

    env = make_env(tmp_path)
    assert _integration_source(SimpleNamespace(), chat_request(), tmp_path) is None
    assert _integration_source(fake_repl(None), chat_request(), tmp_path) is None
    off = fake_repl(env.manager, expose_to_model=False, max_exposed_tools=24)
    assert _integration_source(off, chat_request(), tmp_path) is None


async def test_a_broken_resource_manager_cannot_break_a_chat_turn(tmp_path):
    """Even a manager that raises on every call leaves the native tools and the turn intact."""

    class Boom:
        _workspace = None

        def get(self, *a):
            raise RuntimeError("boom")

        def list_ids(self):
            raise RuntimeError("boom")

    from velune.cli.handlers.tool_chat import _integration_source
    from velune.core.types.inference import InferenceRequest
    from velune.orchestration.tool_loop import ToolLoopRunner

    source = _integration_source(fake_repl(Boom()), chat_request(), tmp_path)
    provider = ScriptedProvider(turn(call("native_echo", "n1", text="hi")), final("fine"))
    runner = ToolLoopRunner(provider, native_registry(), approver=allow_all, dynamic_tools=source)
    request = InferenceRequest(model_id="m", messages=[{"role": "user", "content": "x"}])
    result = await runner.run(request)
    assert result.content == "fine" and result.invocations[0].result == "echo hi"
    assert provider.tool_names[0] == ["native_echo"] == provider.tool_names[1]


def test_the_config_exposes_the_two_switches():
    from velune.kernel.config import ResourcesConfig

    cfg = ResourcesConfig()
    assert cfg.expose_to_model is True and cfg.max_exposed_tools == 24
    with pytest.raises(ValueError):
        ResourcesConfig(max_exposed_tools=2)
    assert "expose_to_model" in cfg.model_dump()
    assert OtherApp  # keep import used
