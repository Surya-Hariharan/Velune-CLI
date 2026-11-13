"""Query execution: read-only by construction, bound, bounded, cancellable (VAIR A5)."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from tests.sql_fakes import (
    SECRET_PASSWORD,
    DriverError,
    OperationalError,
)
from velune.resources.base import ErrorCode, ResourceState


@pytest.fixture
def pg(sql_env):
    sql_env.add("dev")
    return sql_env.pg()


# ── Reads run in an engine-enforced read-only transaction ───────────────────


async def test_a_read_runs_in_a_read_only_transaction_that_is_always_rolled_back(pg, sql_env):
    sql_env.db.on("FROM users", ["id", "name"], [[1, "ada"], [2, "grace"]])
    result = await pg.execute("query", {"sql": "SELECT id, name FROM users"})
    assert result.ok and result.data["rows"] == [[1, "ada"], [2, "grace"]]
    assert result.data["columns"] == ["id", "name"] and result.data["statement"] == "select"
    assert "SET TRANSACTION READ ONLY" in sql_env.db.internal
    assert any(s.startswith("SET LOCAL statement_timeout") for s in sql_env.db.internal)
    assert sql_env.db.commits == 0 and sql_env.db.rollbacks >= 1  # a read never commits


async def test_mysql_reads_also_start_a_read_only_transaction(sql_env):
    sql_env.add("dev", engine="mysql")
    c = sql_env.mysql()
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok
    assert "START TRANSACTION READ ONLY" in sql_env.db.internal
    assert any("sql_select_limit" in s for s in sql_env.db.internal)


async def test_the_engine_stops_a_write_that_slips_past_classification(pg, sql_env):
    # Even if a statement were mis-judged as a read, the server rejects it: prove it by
    # forcing the write through the internal read path.
    analysis_cls = type(pg._dialect_analysis("SELECT 1"))
    read = analysis_cls((), pg._dialect_analysis("SELECT 1").category, "forced", 1)
    pg._dialect_analysis = lambda sql, _r=read: _r  # type: ignore[method-assign]
    result = await pg.execute("query", {"sql": "UPDATE t SET a = 1 WHERE id = 1"})
    assert not result.ok and result.code is ErrorCode.AUTHORIZATION_DENIED
    assert sql_env.db.committed == []


async def test_query_refuses_a_write_with_a_pointer_to_execute(pg, sql_env):
    result = await pg.execute("query", {"sql": "UPDATE t SET a = 1 WHERE id = 1"})
    assert not result.ok and result.code is ErrorCode.UNSUPPORTED_OPERATION
    assert "execute" in result.error and sql_env.db.executed == []


# ── Parameters are bound, never spliced ─────────────────────────────────────


async def test_values_are_passed_as_bound_parameters_and_not_into_the_text(pg, sql_env):
    hostile = "x'; DROP TABLE users; --"
    result = await pg.execute(
        "query", {"sql": "SELECT * FROM users WHERE name = %s AND id = %s", "params": [hostile, 7]}
    )
    assert result.ok
    sql, params = sql_env.db.executed[-1]
    assert "DROP" not in sql and "%s" in sql and params == (hostile, 7)


@pytest.mark.parametrize("bad", [[{"a": 1}], [[1]], "text", 5, [object()]])
async def test_bound_values_must_be_scalars_in_an_array(pg, sql_env, bad):
    result = await pg.execute("query", {"sql": "SELECT %s", "params": bad})
    assert not result.ok and result.code is ErrorCode.VALIDATION_FAILED
    assert sql_env.db.executed == []


async def test_too_many_or_oversized_bound_values_are_refused(pg):
    assert not (await pg.execute("query", {"sql": "SELECT 1", "params": [1] * 65})).ok
    assert not (await pg.execute("query", {"sql": "SELECT %s", "params": ["x" * 10_001]})).ok


async def test_no_params_means_the_driver_gets_none(pg, sql_env):
    await pg.execute("query", {"sql": "SELECT 'a%b'"})
    assert sql_env.db.executed[-1][1] is None  # a literal % is not treated as a placeholder


# ── One statement per call ──────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["query", "explain", "execute"])
async def test_several_statements_in_one_call_are_rejected(pg, sql_env, action):
    result = await pg.execute(action, {"sql": "SELECT 1; SELECT 2"})
    assert not result.ok and result.code is ErrorCode.VALIDATION_FAILED
    assert "exactly one" in result.error and sql_env.db.executed == []


async def test_a_hidden_second_statement_is_still_rejected(pg, sql_env):
    for sql in ("SELECT 1; DROP TABLE t", "SELECT 1 /* x */ ; /* y */ DELETE FROM t"):
        result = await pg.execute("execute", {"sql": sql})
        assert not result.ok and sql_env.db.executed == []


async def test_a_trailing_semicolon_is_one_statement(pg):
    assert (await pg.execute("query", {"sql": "SELECT 1;"})).ok


async def test_unparseable_sql_is_rejected_before_it_reaches_the_server(pg, sql_env):
    result = await pg.execute("query", {"sql": "SELECT 'oops"})
    assert not result.ok and "could not be parsed" in result.error and sql_env.db.executed == []


async def test_administrative_statements_are_denied_and_never_sent(pg, sql_env):
    for sql in (
        "GRANT ALL ON t TO x",
        "SET default_transaction_read_only = off",
        "COPY t TO PROGRAM 'x'",
    ):
        for action in ("query", "execute"):
            result = await pg.execute(action, {"sql": sql})
            assert result.denied and "never run" in result.error
    assert sql_env.db.executed == []


async def test_a_function_that_would_switch_off_read_only_is_blocked(pg, sql_env):
    sql = "SELECT set_config('transaction_read_only', 'off', true)"
    assert (await pg.execute("query", {"sql": sql})).denied and sql_env.db.executed == []


# ── Invalid SQL and server errors ───────────────────────────────────────────


async def test_invalid_sql_is_a_structured_validation_error(pg, sql_env):
    sql_env.db.failures["FROM nowhere"] = DriverError('relation "nowhere" does not exist', "42P01")
    result = await pg.execute("query", {"sql": "SELECT * FROM nowhere"})
    assert not result.ok and result.code is ErrorCode.NOT_FOUND and result.hint
    assert pg.status().state is ResourceState.CONNECTED  # a bad statement is not a bad connection
    assert (await pg.execute("query", {"sql": "SELECT 1"})).ok


async def test_a_failed_one_shot_write_is_rolled_back_not_committed(pg, sql_env):
    sql_env.db.failures["UPDATE t"] = DriverError("duplicate key", "23505")
    result = await pg.execute("execute", {"sql": "UPDATE t SET a = 1 WHERE id = 1"})
    assert not result.ok and result.code is ErrorCode.CONFLICT
    assert sql_env.db.committed == [] and sql_env.db.rollbacks >= 1


# ── Writes: approved path, one statement, committed ─────────────────────────


async def test_execute_commits_an_approved_write(pg, sql_env):
    result = await pg.execute(
        "execute", {"sql": "UPDATE t SET a = %s WHERE id = %s", "params": [1, 2]}
    )
    assert result.ok and result.data["rows_affected"] == 1 and result.data["statement"] == "update"
    assert sql_env.db.committed == ["UPDATE t SET a = %s WHERE id = %s"] and sql_env.db.commits == 1
    assert not any("READ ONLY" in s for s in sql_env.db.internal)  # writes are not read-only


async def test_a_read_only_connection_refuses_every_write(sql_env):
    sql_env.add("ro")
    c = sql_env.pg(sql_env.settings(connections={"ro": {"read_only": True}}))
    result = await c.execute("execute", {"sql": "INSERT INTO t VALUES (1)", "connection": "ro"})
    assert result.denied and "read-only" in result.error and sql_env.db.executed == []
    assert (await c.execute("query", {"sql": "SELECT 1", "connection": "ro"})).ok


# ── Result bounds, metadata, redaction ──────────────────────────────────────


async def test_a_large_result_is_cut_at_the_row_limit_and_says_so(sql_env):
    sql_env.add("dev")
    sql_env.db.on("FROM big", ["n"], [[i] for i in range(1000)])
    c = sql_env.pg(sql_env.settings(max_rows=25))
    result = await c.execute("query", {"sql": "SELECT n FROM big"})
    assert result.ok and result.data["rowcount"] == 25 and result.data["truncated"] is True
    assert result.warnings and "row limit (25)" in result.warnings[0]


async def test_the_call_can_lower_the_row_limit_but_not_raise_it(sql_env):
    sql_env.add("dev")
    sql_env.db.on("FROM big", ["n"], [[i] for i in range(1000)])
    c = sql_env.pg(sql_env.settings(max_rows=50))
    low = await c.execute("query", {"sql": "SELECT n FROM big", "max_rows": 5})
    high = await c.execute("query", {"sql": "SELECT n FROM big", "max_rows": 5000})
    assert low.data["rowcount"] == 5 and high.data["rowcount"] == 50


async def test_the_server_is_asked_for_no_more_than_limit_plus_one_row(pg, sql_env):
    sql_env.db.on("FROM big", ["n"], [[i] for i in range(1000)])
    seen = []
    original = pg._execute_sql

    def spy(conn, sql, params=None, *, limits=None, stream=False):
        seen.append(limits.max_rows if limits else None)
        return original(conn, sql, params, limits=limits, stream=stream)

    pg._execute_sql = spy  # type: ignore[method-assign]
    await pg.execute("query", {"sql": "SELECT n FROM big"})
    assert seen == [200]  # default row limit; fetchmany(limit + 1) happens inside


async def test_results_carry_execution_metadata(pg):
    result = await pg.execute("query", {"sql": "SELECT 1"})
    assert result.data["connection"] == "dev" and result.data["duration_ms"] >= 0
    assert result.to_dict()["ok"] is True


async def test_sensitive_columns_are_masked_in_query_results(pg, sql_env):
    sql_env.db.on(
        "FROM accounts",
        ["id", "email", "password_hash", "api_key"],
        [[1, "a@b.c", "$2b$secret", "sk-1"]],
    )
    result = await pg.execute("query", {"sql": "SELECT * FROM accounts"})
    assert result.data["rows"] == [[1, "a@b.c", "***", "***"]]
    assert result.data["redacted_columns"] == ["password_hash", "api_key"]
    assert "$2b$secret" not in json.dumps(result.to_dict())


async def test_configured_redaction_patterns_apply(sql_env):
    sql_env.add("dev")
    sql_env.db.on("FROM people", ["name", "ssn"], [["ada", "123-45-6789"]])
    c = sql_env.pg(sql_env.settings(redact_columns=["ssn"]))
    result = await c.execute("query", {"sql": "SELECT * FROM people"})
    assert result.data["rows"] == [["ada", "***"]]


async def test_a_driver_error_never_echoes_row_values(pg, sql_env):
    sql_env.db.failures["INSERT"] = DriverError(
        "duplicate key value violates unique constraint\nDETAIL: Key (email)=(a@b.c) already exists.",
        "23505",
    )
    result = await pg.execute("execute", {"sql": "INSERT INTO users VALUES (1)"})
    assert not result.ok and "a@b.c" not in result.error


# ── Timeouts and cancellation ───────────────────────────────────────────────


async def test_a_slow_statement_times_out_is_cancelled_and_its_connection_released(sql_env):
    sql_env.add("dev")
    sql_env.db.block["pg_sleep"] = threading.Event()
    c = sql_env.pg(sql_env.settings(statement_timeout=0.3))
    result = await c.execute("query", {"sql": "SELECT slow FROM pg_sleep"})
    assert not result.ok and result.code is ErrorCode.TIMEOUT and "cancelled" in result.error
    assert sql_env.db.cancelled == 1 and sql_env.db.live == 0  # asked to stop, then released
    sql_env.db.block.clear()
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok  # a fresh connection works
    assert sql_env.db.opened == 2


async def test_the_server_side_statement_timeout_is_set_from_configuration(sql_env):
    sql_env.add("dev")
    c = sql_env.pg(sql_env.settings(statement_timeout=12))
    await c.execute("query", {"sql": "SELECT 1"})
    assert "SET LOCAL statement_timeout = 12000" in sql_env.db.internal


async def test_a_call_may_shorten_but_not_extend_the_timeout(sql_env):
    sql_env.add("dev")
    c = sql_env.pg(sql_env.settings(statement_timeout=10))
    await c.execute("query", {"sql": "SELECT 1", "timeout_seconds": 2})
    await c.execute("query", {"sql": "SELECT 1", "timeout_seconds": 600})
    stamps = [s for s in sql_env.db.internal if "statement_timeout" in s]
    assert stamps == ["SET LOCAL statement_timeout = 2000", "SET LOCAL statement_timeout = 10000"]


async def test_a_timed_out_write_says_its_outcome_is_unknown(sql_env):
    sql_env.add("dev")
    sql_env.db.block["UPDATE slow"] = threading.Event()
    c = sql_env.pg(sql_env.settings(statement_timeout=0.3))
    result = await c.execute("execute", {"sql": "UPDATE slow SET a = 1 WHERE id = 1"})
    assert result.code is ErrorCode.TIMEOUT and "may or may not have been applied" in result.error


async def test_cancelling_the_task_cancels_the_query_and_releases_the_connection(sql_env):
    sql_env.add("dev")
    sql_env.db.block["pg_sleep"] = threading.Event()
    c = sql_env.pg(sql_env.settings(statement_timeout=30))
    task = asyncio.ensure_future(c.execute("query", {"sql": "SELECT slow FROM pg_sleep"}))
    for _ in range(100):
        if sql_env.db.blocked_started.is_set():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sql_env.db.cancelled == 1 and sql_env.db.live == 0


# ── The password never leaves the connector ─────────────────────────────────


async def test_no_executed_text_or_result_contains_the_password(pg, sql_env):
    sql_env.db.failures["boom"] = OperationalError(f"password={SECRET_PASSWORD} rejected")
    out = await pg.execute("query", {"sql": "SELECT boom"})
    seen = repr(out.to_dict()) + repr(sql_env.db.executed) + repr(sql_env.db.internal)
    assert SECRET_PASSWORD not in seen


# ── Streaming reads use a server-side cursor on psycopg 3 only ──────────────


def _fake_driver_connection(module: str):
    """A connection whose class claims *module*, recording the cursors it hands out."""
    made: list[dict] = []

    class Conn:
        def cursor(self, name=None):
            made.append({"name": name})
            return object()

    Conn.__module__ = module
    return Conn(), made


def test_a_select_streams_through_a_server_side_cursor_on_psycopg3(sql_env):
    pg = sql_env.pg()
    for module in ("psycopg", "psycopg.connection"):  # what the real driver reports: "psycopg"
        conn, made = _fake_driver_connection(module)
        pg._open_cursor(conn, stream=True)
        assert made == [{"name": "velune_cursor"}]


def test_psycopg2_and_other_connections_never_get_a_named_cursor(sql_env):
    pg = sql_env.pg()
    for module in ("psycopg2.extensions", "psycopg2", "tests.sql_fakes", "builtins"):
        conn, made = _fake_driver_connection(module)
        pg._open_cursor(conn, stream=True)
        assert made == [{"name": None}]
    conn, made = _fake_driver_connection("psycopg")
    pg._open_cursor(conn, stream=False)  # only read statements stream
    assert made == [{"name": None}]


async def test_only_plain_selects_ask_for_streaming(sql_env):
    sql_env.add("dev")
    pg = sql_env.pg()
    wanted: list[bool] = []
    real = pg._open_cursor

    def spy(conn, *, stream):
        wanted.append(stream)
        return real(conn, stream=stream)

    pg._open_cursor = spy  # type: ignore[method-assign]
    await pg.execute("query", {"sql": "SELECT 1"})
    await pg.execute("query", {"sql": "WITH x AS (SELECT 1) SELECT * FROM x"})
    await pg.execute("query", {"sql": "SHOW server_version"})
    await pg.execute("explain", {"sql": "SELECT 1"})
    assert wanted == [True, True, False, False]
