"""Explicit transactions: commit, rollback, failure, loss, idle expiry, cleanup (VAIR A5)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tests.sql_fakes import (
    DriverError,
    OperationalError,
)
from velune.resources.base import ErrorCode

UPDATE = "UPDATE accounts SET balance = balance - 10 WHERE id = %s"
INSERT = "INSERT INTO ledger (account, amount) VALUES (%s, %s)"


@pytest.fixture
def pg(sql_env):
    sql_env.add("dev")
    return sql_env.pg()


async def begin(pg, **kw):
    result = await pg.execute("transaction_begin", kw)
    assert result.ok, result.error
    return result.data["transaction_id"]


# ── Commit ──────────────────────────────────────────────────────────────────


async def test_statements_commit_together_only_on_commit(pg, sql_env):
    tid = await begin(pg)
    assert (
        await pg.execute(
            "transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]}
        )
    ).ok
    assert (
        await pg.execute(
            "transaction_execute", {"transaction_id": tid, "sql": INSERT, "params": [1, -10]}
        )
    ).ok
    assert sql_env.db.committed == [] and sql_env.db.commits == 0  # nothing is saved yet
    done = await pg.execute("transaction_commit", {"transaction_id": tid})
    assert done.ok and done.data["committed"] and done.data["statements"] == 2
    assert sql_env.db.committed == [UPDATE, INSERT] and sql_env.db.commits == 1


async def test_a_transaction_reuses_one_connection_and_blocks_other_one_shot_calls(pg, sql_env):
    tid = await begin(pg)
    other = await pg.execute("query", {"sql": "SELECT 1"})
    assert not other.ok and other.code is ErrorCode.CONFLICT and tid in other.error
    await pg.execute("transaction_rollback", {"transaction_id": tid})
    assert (await pg.execute("query", {"sql": "SELECT 1"})).ok


async def test_only_one_transaction_per_connection(pg):
    await begin(pg)
    second = await pg.execute("transaction_begin", {})
    assert not second.ok and second.code is ErrorCode.CONFLICT


async def test_a_read_inside_a_transaction_returns_rows(pg, sql_env):
    sql_env.db.on("FROM accounts", ["id", "balance"], [[1, 90]])
    tid = await begin(pg)
    out = await pg.execute(
        "transaction_execute", {"transaction_id": tid, "sql": "SELECT id, balance FROM accounts"}
    )
    assert out.ok and out.data["rows"] == [[1, 90]] and out.data["transaction_id"] == tid


# ── Rollback ────────────────────────────────────────────────────────────────


async def test_rollback_discards_everything(pg, sql_env):
    tid = await begin(pg)
    await pg.execute("transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]})
    out = await pg.execute("transaction_rollback", {"transaction_id": tid})
    assert out.ok and out.data["statements_discarded"] == 1
    assert sql_env.db.committed == [] and sql_env.db.rolled_back == [UPDATE]


async def test_a_finished_transaction_cannot_be_used_again(pg):
    tid = await begin(pg)
    await pg.execute("transaction_commit", {"transaction_id": tid})
    for action, extra in (
        ("transaction_execute", {"sql": "SELECT 1"}),
        ("transaction_commit", {}),
        ("transaction_rollback", {}),
    ):
        result = await pg.execute(action, {"transaction_id": tid, **extra})
        assert not result.ok and result.code is ErrorCode.NOT_FOUND
    unknown = await pg.execute("transaction_commit", {"transaction_id": "nope"})
    assert not unknown.ok and "Nothing was committed" in unknown.error


# ── A failed statement poisons the transaction: no accidental commit ────────


async def test_after_a_failed_statement_commit_is_refused_and_rolls_back(pg, sql_env):
    tid = await begin(pg)
    await pg.execute("transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]})
    sql_env.db.failures["INSERT"] = DriverError("duplicate key", "23505")
    failed = await pg.execute(
        "transaction_execute", {"transaction_id": tid, "sql": INSERT, "params": [1, 2]}
    )
    assert not failed.ok and "transaction_rollback" in failed.hint
    again = await pg.execute("transaction_execute", {"transaction_id": tid, "sql": "SELECT 1"})
    assert not again.ok and again.code is ErrorCode.CONFLICT  # the server would refuse too
    commit = await pg.execute("transaction_commit", {"transaction_id": tid})
    assert not commit.ok and "Refusing to commit" in commit.error
    assert sql_env.db.committed == [] and sql_env.db.commits == 0
    assert (await pg.execute("query", {"sql": "SELECT 1"})).ok  # the connection is free again


async def test_a_failed_statement_can_be_rolled_back_explicitly(pg, sql_env):
    tid = await begin(pg)
    sql_env.db.failures["INSERT"] = DriverError("boom", "23505")
    await pg.execute(
        "transaction_execute", {"transaction_id": tid, "sql": INSERT, "params": [1, 2]}
    )
    assert (await pg.execute("transaction_rollback", {"transaction_id": tid})).ok


# ── Statement rules inside a transaction ────────────────────────────────────


async def test_transaction_control_and_admin_statements_are_refused_inside(pg, sql_env):
    tid = await begin(pg)
    for sql in (
        "COMMIT",
        "ROLLBACK",
        "BEGIN",
        "SAVEPOINT x",
        "SET ROLE admin",
        "GRANT ALL ON t TO x",
    ):
        result = await pg.execute("transaction_execute", {"transaction_id": tid, "sql": sql})
        assert result.denied, sql
    assert sql_env.db.executed == []


async def test_one_statement_per_call_inside_a_transaction_too(pg, sql_env):
    tid = await begin(pg)
    result = await pg.execute(
        "transaction_execute", {"transaction_id": tid, "sql": "SELECT 1; SELECT 2"}
    )
    assert not result.ok and "exactly one" in result.error


async def test_a_read_only_transaction_cannot_change_anything(pg, sql_env):
    tid = await begin(pg, read_only=True)
    assert "SET TRANSACTION READ ONLY" in sql_env.db.internal
    denied = await pg.execute(
        "transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]}
    )
    assert denied.denied and "read-only" in denied.error and sql_env.db.executed == []
    assert (await pg.execute("transaction_execute", {"transaction_id": tid, "sql": "SELECT 1"})).ok


async def test_a_read_only_connection_cannot_begin_a_writable_transaction(sql_env):
    sql_env.add("ro")
    c = sql_env.pg(sql_env.settings(connections={"ro": {"read_only": True}}))
    assert (await c.execute("transaction_begin", {"connection": "ro"})).denied
    assert (await c.execute("transaction_begin", {"connection": "ro", "read_only": True})).ok


# ── Connection loss ────────────────────────────────────────────────────────


async def test_losing_the_connection_mid_transaction_loses_the_transaction(pg, sql_env):
    tid = await begin(pg)
    await pg.execute("transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]})
    sql_env.db.kill_all = True
    lost = await pg.execute(
        "transaction_execute", {"transaction_id": tid, "sql": INSERT, "params": [1, 2]}
    )
    assert not lost.ok and lost.code is ErrorCode.CONNECTION_FAILED
    assert "nothing was committed" in lost.error
    sql_env.db.revive()
    gone = await pg.execute("transaction_commit", {"transaction_id": tid})
    assert (
        not gone.ok and gone.code is ErrorCode.NOT_FOUND and "Nothing was committed" in gone.error
    )
    assert sql_env.db.committed == [] and sql_env.db.live == 0


async def test_a_connection_lost_during_commit_says_the_outcome_is_unknown(pg, sql_env):
    tid = await begin(pg)
    await pg.execute("transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]})
    sql_env.db.commit_error = OperationalError("connection lost during commit")
    out = await pg.execute("transaction_commit", {"transaction_id": tid})
    assert not out.ok and "UNKNOWN" in out.error and out.code is ErrorCode.CONNECTION_FAILED
    assert sql_env.db.live == 0  # no half-open connection left behind


async def test_a_failing_commit_rolls_back_and_releases(pg, sql_env):
    tid = await begin(pg)
    await pg.execute("transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]})
    sql_env.db.commit_error = DriverError("deferred constraint violated", "23505")
    out = await pg.execute("transaction_commit", {"transaction_id": tid})
    assert not out.ok and "rolled back" in out.error and sql_env.db.committed == []
    sql_env.db.commit_error = None
    assert (await pg.execute("query", {"sql": "SELECT 1"})).ok  # usable again


async def test_a_statement_timeout_inside_a_transaction_rolls_it_back(sql_env):
    sql_env.add("dev")
    sql_env.db.block["slow"] = threading.Event()
    c = sql_env.pg(sql_env.settings(statement_timeout=0.3))
    tid = await begin(c)
    out = await c.execute(
        "transaction_execute", {"transaction_id": tid, "sql": "SELECT slow FROM t"}
    )
    assert not out.ok and out.code is ErrorCode.TIMEOUT and "nothing was committed" in out.error
    assert sql_env.db.live == 0 and sql_env.db.committed == []


# ── Nothing is left open ───────────────────────────────────────────────────


async def test_disconnect_rolls_back_an_open_transaction(pg, sql_env):
    tid = await begin(pg)
    await pg.execute("transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]})
    await pg.disconnect()
    assert (
        sql_env.db.committed == [] and sql_env.db.rolled_back == [UPDATE] and sql_env.db.live == 0
    )


async def test_an_idle_transaction_is_rolled_back_by_itself(sql_env):
    sql_env.add("dev")
    c = sql_env.pg(sql_env.settings(idle_transaction_timeout=0.2))
    tid = await begin(c)
    await c.execute("transaction_execute", {"transaction_id": tid, "sql": UPDATE, "params": [1]})
    await asyncio.sleep(0.6)
    assert sql_env.db.rolled_back == [UPDATE] and sql_env.db.committed == []
    gone = await c.execute("transaction_commit", {"transaction_id": tid})
    assert not gone.ok and gone.code is ErrorCode.NOT_FOUND
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok


async def test_activity_keeps_a_transaction_alive(sql_env):
    sql_env.add("dev")
    c = sql_env.pg(sql_env.settings(idle_transaction_timeout=0.5))
    tid = await begin(c)
    for _ in range(3):
        await asyncio.sleep(0.25)
        assert (
            await c.execute("transaction_execute", {"transaction_id": tid, "sql": "SELECT 1"})
        ).ok
    assert (await c.execute("transaction_commit", {"transaction_id": tid})).ok


async def test_transactions_on_different_connections_are_independent(sql_env):
    sql_env.add("a")
    sql_env.add("b")
    c = sql_env.pg()
    ta = (await c.execute("transaction_begin", {"connection": "a"})).data["transaction_id"]
    tb = (await c.execute("transaction_begin", {"connection": "b"})).data["transaction_id"]
    await c.execute("transaction_execute", {"transaction_id": ta, "sql": UPDATE, "params": [1]})
    await c.execute("transaction_rollback", {"transaction_id": ta})
    assert (await c.execute("transaction_commit", {"transaction_id": tb})).ok
    assert sql_env.db.committed == []


async def test_mysql_transactions_work_the_same_way(sql_env):
    sql_env.add("dev", engine="mysql")
    c = sql_env.mysql()
    tid = await begin(c)
    assert (
        await c.execute(
            "transaction_execute", {"transaction_id": tid, "sql": "UPDATE t SET a = 1 WHERE id = 1"}
        )
    ).ok
    assert (await c.execute("transaction_commit", {"transaction_id": tid})).ok
    assert sql_env.db.committed == ["UPDATE t SET a = 1 WHERE id = 1"]


async def test_a_connection_released_before_the_transaction_starts_is_reopened(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok
    session = c._sessions["dev"]
    real_ready = c._ready

    async def ready_then_lose_it(name):
        got = await real_ready(name)
        await c._drop(session, rollback=False)
        return got

    c._ready = ready_then_lose_it  # type: ignore[method-assign]
    begun = await c.execute("transaction_begin", {})
    assert begun.ok and sql_env.db.opened == 2
    await c.execute("transaction_rollback", {"transaction_id": begun.data["transaction_id"]})
