"""Database permissions, A2 discovery and the real tool loop (VAIR A5)."""

from __future__ import annotations

import json

import pytest

from tests.http_env import decide, verdict
from tests.integration_fakes import ScriptedProvider, call, final, make_env, turn
from tests.sql_fakes import (
    SECRET_PASSWORD,
)
from velune.permissions.actions import ActionType, Risk
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode
from velune.resources.capabilities import build_manifest

MANUAL, AUTO, PLAN = ExecutionMode.MANUAL, ExecutionMode.AUTO, ExecutionMode.PLAN
UPDATE = "UPDATE accounts SET balance = 0 WHERE id = 1"


@pytest.fixture
def pg(sql_env):
    sql_env.add("dev", environment="development")
    return sql_env.pg()


def v(conn, action, params, mode, tmp_path):
    return verdict(conn, action, params, mode, tmp_path)


def all_modes(conn, action, params, tmp_path):
    return tuple(v(conn, action, params, m, tmp_path) for m in (MANUAL, PLAN, AUTO))


# ── The matrix: reads, writes, schema changes, destructive, blocked ─────────


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("query", {"sql": "SELECT * FROM users WHERE id = %s", "params": [1]}),
        ("explain", {"sql": "SELECT 1"}),
        ("list_tables", {}),
        ("describe_table", {"table": "users"}),
        ("database_info", {}),
        ("list_connections", {}),
        ("health", {}),
        ("diagnose", {}),
        ("transaction_begin", {"read_only": True}),
        ("transaction_rollback", {"transaction_id": "x"}),
    ],
)
def test_reads_never_need_a_person_in_any_mode(pg, tmp_path, action, params):
    assert all_modes(pg, action, params, tmp_path) == ("allow", "allow", "allow")


@pytest.mark.parametrize(
    "sql",
    [
        UPDATE,
        "INSERT INTO t VALUES (1)",
        "DELETE FROM t WHERE id = 1",
        "CREATE TABLE t (id int)",
        "ALTER TABLE t ADD COLUMN c int",
    ],
)
def test_data_and_schema_changes_ask_in_manual_and_auto_and_are_denied_in_plan(pg, tmp_path, sql):
    # AUTO does not run changes unattended: a database is never assumed disposable.
    assert all_modes(pg, "execute", {"sql": sql}, tmp_path) == ("ask", "deny", "ask")


@pytest.mark.parametrize("sql", ["DROP TABLE t", "TRUNCATE t", "ALTER TABLE t DROP COLUMN c"])
def test_destructive_statements_ask_in_manual_and_auto_and_are_denied_in_plan(pg, tmp_path, sql):
    assert all_modes(pg, "execute", {"sql": sql}, tmp_path) == ("ask", "deny", "ask")
    actions = decide(pg, "execute", {"sql": sql}, AUTO, tmp_path)[1]
    assert actions[0].action_type is ActionType.INTEGRATION_DELETE and actions[0].risk is Risk.HIGH


@pytest.mark.parametrize(
    "sql",
    [
        "GRANT ALL ON t TO x",
        "SET default_transaction_read_only = off",
        "COPY t TO PROGRAM 'cat'",
        "CREATE ROLE evil",
        "DO $$ BEGIN END $$",
        "SELECT pg_terminate_backend(1)",
        "COMMIT",
    ],
)
@pytest.mark.parametrize("action", ["query", "execute", "transaction_execute"])
def test_administrative_statements_are_denied_in_every_mode(pg, tmp_path, sql, action):
    params = (
        {"sql": sql, "transaction_id": "x"} if action == "transaction_execute" else {"sql": sql}
    )
    assert all_modes(pg, action, params, tmp_path) == ("deny", "deny", "deny")


def test_query_is_read_only_a_write_through_it_is_refused_not_offered_for_approval(pg, tmp_path):
    assert all_modes(pg, "query", {"sql": UPDATE}, tmp_path) == ("deny", "deny", "deny")
    action = decide(pg, "query", {"sql": UPDATE}, MANUAL, tmp_path)[1][0]
    assert action.blocked and "use execute" in action.detail


@pytest.mark.parametrize("sql", ["SELECT 1; SELECT 2", "SELECT 1; DROP TABLE t", "", "SELECT 'x"])
def test_anything_but_exactly_one_parseable_statement_is_denied(pg, tmp_path, sql):
    for action in ("query", "execute"):
        assert all_modes(pg, action, {"sql": sql}, tmp_path) == ("deny", "deny", "deny")


def test_a_hidden_statement_cannot_ride_in_under_a_read(pg, tmp_path):
    hidden_batch = "SELECT 1 /* x */; /* y */ DROP TABLE t"
    assert all_modes(pg, "query", {"sql": hidden_batch}, tmp_path) == ("deny", "deny", "deny")
    assert all_modes(pg, "execute", {"sql": hidden_batch}, tmp_path) == ("deny", "deny", "deny")
    for sql in (
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "EXPLAIN ANALYZE DELETE FROM t",
        "SELECT * INTO copy FROM t",
    ):
        assert v(pg, "query", {"sql": sql}, AUTO, tmp_path) == "deny"  # not a read
        assert v(pg, "execute", {"sql": sql}, AUTO, tmp_path) == "ask"  # a change, so a person


def test_explain_cannot_be_used_to_run_a_write(pg, tmp_path):
    # explain prepends EXPLAIN itself: ANALYZE in the text would run the statement
    assert v(pg, "explain", {"sql": "ANALYZE DELETE FROM t"}, AUTO, tmp_path) == "deny"
    assert v(pg, "explain", {"sql": "DELETE FROM t"}, AUTO, tmp_path) == "allow"  # planned only


# ── Precise scopes ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "params", "scope"),
    [
        ("query", {"sql": "SELECT 1"}, "database.query.read"),
        ("explain", {"sql": "SELECT 1"}, "database.query.read"),
        ("list_tables", {}, "database.schema.read"),
        ("describe_table", {"table": "t"}, "database.schema.read"),
        ("health", {}, "database.health"),
        ("list_connections", {}, "database.connection.read"),
        ("execute", {"sql": UPDATE}, "database.query.write"),
        ("execute", {"sql": "CREATE TABLE t (a int)"}, "database.schema.write"),
        ("execute", {"sql": "DROP TABLE t"}, "database.query.destructive"),
        ("transaction_begin", {}, "database.transaction"),
        ("transaction_commit", {"transaction_id": "x"}, "database.transaction"),
        ("transaction_rollback", {"transaction_id": "x"}, "database.transaction"),
        ("transaction_execute", {"transaction_id": "x", "sql": "SELECT 1"}, "database.transaction"),
        ("transaction_execute", {"transaction_id": "x", "sql": UPDATE}, "database.query.write"),
        (
            "transaction_execute",
            {"transaction_id": "x", "sql": "DROP TABLE t"},
            "database.query.destructive",
        ),
    ],
)
def test_every_operation_names_its_precise_scope(pg, action, params, scope):
    assert pg.describe_actions(action, params)[0].scope == scope


def test_unknown_or_missing_scopes_fail_closed(pg, tmp_path):
    pg.scope_for = lambda *a, **k: ""  # type: ignore[method-assign]
    assert all_modes(pg, "query", {"sql": "SELECT 1"}, tmp_path) == ("deny", "deny", "deny")
    pg.scope_for = lambda *a, **k: "Not A Scope!"  # type: ignore[method-assign]
    assert v(pg, "list_tables", {}, AUTO, tmp_path) == "deny"


def test_a_connector_that_cannot_describe_a_call_is_denied(pg, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("cannot describe")

    pg.describe_actions = boom  # type: ignore[method-assign]
    assert v(pg, "query", {"sql": "SELECT 1"}, AUTO, tmp_path) == "deny"


# ── Transactions ────────────────────────────────────────────────────────────


def test_beginning_a_writable_transaction_is_a_change_a_read_only_one_is_not(pg, tmp_path):
    assert all_modes(pg, "transaction_begin", {}, tmp_path) == ("ask", "deny", "ask")
    assert all_modes(pg, "transaction_begin", {"read_only": True}, tmp_path) == (
        "allow",
        "allow",
        "allow",
    )


def test_each_statement_in_a_transaction_is_judged_on_its_own(pg, tmp_path):
    base = {"transaction_id": "x"}
    assert all_modes(pg, "transaction_execute", {**base, "sql": "SELECT 1"}, tmp_path)[2] == "allow"
    assert all_modes(pg, "transaction_execute", {**base, "sql": UPDATE}, tmp_path) == (
        "ask",
        "deny",
        "ask",
    )
    assert all_modes(pg, "transaction_execute", {**base, "sql": "DROP TABLE t"}, tmp_path) == (
        "ask",
        "deny",
        "ask",
    )


def test_committing_a_transaction_that_may_have_changed_data_asks(pg, tmp_path):
    assert all_modes(pg, "transaction_commit", {"transaction_id": "unknown"}, tmp_path) == (
        "ask",
        "deny",
        "ask",
    )


# ── Connection policy ───────────────────────────────────────────────────────


def test_a_read_only_connection_blocks_changes_in_every_mode(sql_env, tmp_path):
    sql_env.add("ro")
    c = sql_env.pg(sql_env.settings(connections={"ro": {"read_only": True}}))
    p = {"connection": "ro"}
    assert all_modes(c, "execute", {"sql": UPDATE, **p}, tmp_path) == ("deny", "deny", "deny")
    assert all_modes(c, "transaction_begin", p, tmp_path) == ("deny", "deny", "deny")
    assert all_modes(c, "query", {"sql": "SELECT 1", **p}, tmp_path) == ("allow", "allow", "allow")


def test_a_connection_called_development_is_not_assumed_disposable(sql_env, tmp_path):
    sql_env.add("test-db", environment="test")
    c = sql_env.pg()
    assert v(c, "execute", {"sql": UPDATE, "connection": "test-db"}, AUTO, tmp_path) == "ask"


def test_production_and_remote_connections_say_why(sql_env, tmp_path):
    sql_env.add("prod", environment="production")
    action = sql_env.pg().describe_actions("execute", {"sql": UPDATE, "connection": "prod"})[0]
    assert "production connection" in action.reason
    assert (
        v(sql_env.pg(), "execute", {"sql": UPDATE, "connection": "prod"}, AUTO, tmp_path) == "ask"
    )


def test_an_unbounded_change_is_called_out(pg):
    action = pg.describe_actions("execute", {"sql": "DELETE FROM accounts"})[0]
    assert "affects every row" in action.reason


# ── What the person approving sees ──────────────────────────────────────────


def test_the_approval_preview_names_the_connection_database_and_statement_safely(pg):
    action = pg.describe_actions(
        "execute", {"sql": "UPDATE accounts SET balance = %s WHERE id = %s", "params": [0, 7]}
    )[0]
    assert "connection=dev" in action.detail and "db=app" in action.detail
    assert "env=development" in action.detail and "UPDATE accounts" in action.detail
    assert "2 bound values" in action.detail
    assert SECRET_PASSWORD not in action.detail and SECRET_PASSWORD not in repr(action)


def test_the_preview_never_contains_bound_values(pg):
    action = pg.describe_actions(
        "execute", {"sql": "UPDATE t SET a = %s WHERE id = 1", "params": ["hunter2-private"]}
    )[0]
    assert "hunter2-private" not in action.detail


# ── A2: discovery exposes, never authorizes ─────────────────────────────────


def test_the_manifest_groups_operations_into_the_documented_categories(pg):
    manifest = build_manifest(pg)
    by_cat: dict[str, set[str]] = {}
    for op in manifest.operations:
        by_cat.setdefault(op.category, set()).add(op.action)
    assert set(by_cat) == {"connections", "schema", "query_read", "query_write", "transactions"}
    assert by_cat["query_read"] == {"query", "explain"} and by_cat["query_write"] == {"execute"}
    assert by_cat["transactions"] == {
        "transaction_begin",
        "transaction_execute",
        "transaction_commit",
        "transaction_rollback",
    }
    assert by_cat["connections"] == {"list_connections", "health", "diagnose"}


def test_tool_definitions_stay_compact(sql_env):
    sql_env.add("dev")
    env = make_env(sql_env.tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()})
    env.source.catalog  # noqa: B018 - warm the manifest
    manifest = env.source.catalog.manifest("postgres")
    sizes = {
        op.action: len(json.dumps({"d": op.description, "s": op.schema}))
        for op in manifest.operations
    }
    assert max(sizes.values()) < 1500 and sum(sizes.values()) < 14000
    assert all(len(op.description) <= 300 for op in manifest.operations)


async def test_enabling_schema_inspection_exposes_only_schema_tools(tmp_path, sql_env):
    sql_env.add("dev")
    env = make_env(tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()})
    provider = ScriptedProvider(
        turn(call("integrations_list", "l1")),
        turn(call("integrations_enable", "e1", integration="postgres", categories=["schema"])),
        final("ok"),
    )
    result = await env.run(provider, "what tables are there?")
    row = {r["id"]: r for r in json.loads(result.invocations[0].result)["integrations"]}["postgres"]
    assert {c["name"] for c in row["categories"]} == {
        "connections",
        "schema",
        "query_read",
        "query_write",
        "transactions",
    }
    names = provider.tool_names[2]
    assert "postgres__list_tables" in names and "postgres__describe_table" in names
    assert not {
        n
        for n in names
        if n.startswith("postgres__")
        and n
        in {
            "postgres__execute",
            "postgres__query",
            "postgres__transaction_begin",
        }
    }


async def test_discovery_and_enabling_grant_no_authority(tmp_path, sql_env):
    sql_env.add("dev")
    asked: list = []

    async def ask(tool, decision):
        asked.append(decision)
        return Approval.DENY

    env = make_env(tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()}, ask=ask)
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="postgres")),
        turn(call("postgres__execute", "x1", sql=UPDATE)),
        turn(call("postgres__query", "q1", sql="SELECT 1")),
        final("done"),
    )
    result = await env.run(provider, "zero the balance")
    write = result.invocations[1].result
    assert "was not allowed" in write and "rejected" in write
    assert len(asked) == 1 and "database.query.write" in asked[0].reason + str(asked[0])
    assert sql_env.db.committed == [] and UPDATE not in sql_env.db.user_sql()
    assert json.loads(result.invocations[2].result)["ok"] is True  # the read needed no one


async def test_an_approved_change_runs_exactly_once_through_the_gate(tmp_path, sql_env):
    sql_env.add("dev")
    asked: list = []

    async def ask(tool, decision):
        asked.append(decision)
        return Approval.ALLOW_ONCE

    env = make_env(tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()}, ask=ask)
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="postgres")),
        turn(call("postgres__execute", "x1", sql=UPDATE)),
        final("done"),
    )
    await env.run(provider, "zero the balance")
    assert sql_env.db.committed == [UPDATE] and len(asked) == 1
    assert any(r for r in env.records if "database.query.write" in json.dumps(r, default=str))


async def test_a_session_grant_never_covers_a_database_change(tmp_path, sql_env):
    sql_env.add("dev")
    asked: list = []

    async def ask(tool, decision):
        asked.append(decision)
        return Approval.ALLOW_TASK

    env = make_env(tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()}, ask=ask)
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="postgres")),
        turn(call("postgres__execute", "x1", sql=UPDATE)),
        turn(call("postgres__execute", "x2", sql=UPDATE)),
        final("done"),
    )
    await env.run(provider, "twice")
    assert len(asked) == 2  # "always allow" is not honored for high-risk changes


async def test_plan_mode_executes_no_mutation_and_explains(tmp_path, sql_env):
    sql_env.add("dev")
    env = make_env(tmp_path, PLAN, connectors={"postgres": lambda: sql_env.pg()})
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="postgres")),
        turn(call("postgres__execute", "x1", sql=UPDATE)),
        turn(call("postgres__transaction_begin", "t1")),
        final("planned"),
    )
    result = await env.run(provider, "zero the balance")
    for index in (1, 2):
        out = result.invocations[index].result
        assert "was not allowed" in out and "plan mode" in out and "write_plan" in out
    assert sql_env.db.executed == [] and sql_env.db.opened == 0


async def test_the_manager_path_cannot_bypass_the_gate(tmp_path, sql_env):
    sql_env.add("dev")
    env = make_env(tmp_path, PLAN, connectors={"postgres": lambda: sql_env.pg()})
    result = await env.manager.execute("postgres", "execute", {"sql": UPDATE})
    assert result.denied and sql_env.db.executed == []
    # a statement that is blocked is refused even in AUTO with a permissive approver
    auto = make_env(tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()})
    assert (
        await auto.manager.execute("postgres", "execute", {"sql": "GRANT ALL ON t TO x"})
    ).denied


async def test_a_full_read_workflow_through_the_loop(tmp_path, sql_env):
    sql_env.add("dev")
    sql_env.db.on(
        "FROM information_schema.tables", ["schema", "name", "kind"], [["public", "users", "table"]]
    )
    sql_env.db.on("FROM users", ["id", "email"], [[1, "ada@example.com"]])
    env = make_env(tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()})
    provider = ScriptedProvider(
        turn(
            call(
                "integrations_enable",
                "e1",
                integration="postgres",
                categories=["schema", "query_read"],
            )
        ),
        turn(call("postgres__list_tables", "t1")),
        turn(
            call(
                "postgres__query", "q1", sql="SELECT id, email FROM users WHERE id = %s", params=[1]
            )
        ),
        final("Ada is user 1."),
    )
    result = await env.run(provider, "who is user 1?")
    tables = json.loads(result.invocations[1].result)
    rows = json.loads(result.invocations[2].result)
    assert tables["ok"] and tables["data"]["tables"][0]["name"] == "users"
    assert rows["ok"] and rows["data"]["rows"] == [[1, "ada@example.com"]]
    assert sql_env.db.executed[-1][1] == (1,) and sql_env.db.committed == []
    assert SECRET_PASSWORD not in json.dumps([i.result for i in result.invocations])


async def test_operations_beyond_the_tool_budget_reach_the_database_through_the_same_gate(
    tmp_path, sql_env
):
    sql_env.add("dev")
    asked: list = []

    async def ask(tool, decision):
        asked.append((tool, decision))
        return Approval.DENY

    env = make_env(
        tmp_path, AUTO, connectors={"postgres": lambda: sql_env.pg()}, ask=ask, max_tools=5
    )
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="postgres")),
        turn(
            call(
                "integration_call",
                "c1",
                integration="postgres",
                operation="execute",
                arguments=json.dumps({"sql": UPDATE}),
            )
        ),
        turn(
            call(
                "integration_call",
                "c2",
                integration="postgres",
                operation="query",
                arguments=json.dumps({"sql": "SELECT 1"}),
            )
        ),
        final("done"),
    )
    result = await env.run(provider, "change then read")
    enable = json.loads(result.invocations[0].result)
    assert enable["via_integration_call"]  # some operations are dispatched, not direct
    assert "was not allowed" in result.invocations[1].result
    assert [tool for tool, _ in asked] == [
        "postgres__execute"
    ]  # authorized as the operation itself
    assert json.loads(result.invocations[2].result)["ok"] is True
    assert sql_env.db.committed == []
