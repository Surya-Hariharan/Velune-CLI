"""Named connections, policy, reconnects, health and diagnosis (VAIR A5)."""

from __future__ import annotations

import asyncio

import pytest

from tests.sql_fakes import (
    SECRET_PASSWORD,
    DriverError,
    OperationalError,
)
from velune.resources.base import ErrorCode, ResourceState
from velune.resources.sql_base import classify_db_error

# ── Several named connections ───────────────────────────────────────────────


async def test_one_connector_serves_several_named_connections(sql_env):
    sql_env.add("dev", database="app_dev")
    sql_env.add("staging", database="app_staging")
    c = sql_env.pg()
    listing = await c.execute("list_connections", {})
    names = [x["name"] for x in listing.data["connections"]]
    assert names == ["dev", "staging"] and listing.ok
    assert (await c.execute("query", {"sql": "SELECT 1", "connection": "staging"})).ok
    assert (await c.execute("query", {"sql": "SELECT 1", "connection": "dev"})).ok
    assert {cfg["database"] for cfg in sql_env.db.configs} == {"app_dev", "app_staging"}


async def test_an_ambiguous_default_is_an_error_not_a_guess(sql_env):
    sql_env.add("dev")
    sql_env.add("staging")
    result = await sql_env.pg().execute("query", {"sql": "SELECT 1"})
    assert not result.ok and result.code is ErrorCode.VALIDATION_FAILED
    assert "dev" in result.error and "staging" in result.error
    assert sql_env.db.opened == 0  # nothing was opened on a guess


async def test_a_configured_default_is_used_when_none_is_named(sql_env):
    sql_env.add("dev", database="app_dev")
    sql_env.add("staging", database="app_staging")
    c = sql_env.pg(sql_env.settings(default_connection="staging"))
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok
    assert sql_env.db.configs[0]["database"] == "app_staging"
    listing = await c.execute("list_connections", {})
    assert [x["name"] for x in listing.data["connections"] if x["default"]] == ["staging"]


async def test_an_unknown_connection_name_is_refused_and_listed(sql_env):
    sql_env.add("dev")
    result = await sql_env.pg().execute("query", {"sql": "SELECT 1", "connection": "prod"})
    assert not result.ok and result.code is ErrorCode.NOT_FOUND and "dev" in result.error
    bad = await sql_env.pg().execute("query", {"sql": "SELECT 1", "connection": "x; drop"})
    assert not bad.ok and bad.code is ErrorCode.NOT_FOUND


async def test_the_legacy_single_connection_still_works(sql_env):
    sql_env.add("postgres")  # the name /resource configure postgres has always used
    assert (await sql_env.pg().execute("query", {"sql": "SELECT 1"})).ok


# ── Credentials: references, never embedded, never shown ────────────────────


async def test_policy_comes_from_configuration_but_the_endpoint_stays_in_the_keystore(sql_env):
    sql_env.add("pg-prod", host="localhost", database="real_db")
    settings = sql_env.settings(
        connections={
            "prod": {
                "credential": "pg-prod",
                "environment": "production",
                "read_only": True,
                "host": "attacker.example",  # a repository trying to redirect the stored password
                "database": "elsewhere",
            }
        }
    )
    c = sql_env.pg(settings)
    assert (await c.execute("query", {"sql": "SELECT 1", "connection": "prod"})).ok
    cfg = sql_env.db.configs[0]
    assert cfg["host"] == "localhost" and cfg["database"] == "real_db"  # from the keystore
    profile, _ = c._profile_for("prod")
    assert profile.read_only and profile.environment == "production"


async def test_an_endpoint_in_configuration_is_used_only_with_no_stored_password(sql_env):
    settings = sql_env.settings(
        connections={"scratch": {"host": "localhost", "database": "scratch", "username": "me"}}
    )
    c = sql_env.pg(settings)
    assert (await c.execute("query", {"sql": "SELECT 1", "connection": "scratch"})).ok
    cfg = sql_env.db.configs[0]
    assert cfg["database"] == "scratch" and "password" not in cfg
    untrusted = sql_env.pg(settings, workspace_trusted=False)
    missing = await untrusted.execute("query", {"sql": "SELECT 1", "connection": "scratch"})
    assert not missing.ok  # an untrusted workspace's endpoint is ignored


async def test_a_missing_credential_is_a_clear_configuration_error(sql_env):
    result = await sql_env.pg().execute("query", {"sql": "SELECT 1"})
    assert not result.ok and "/resource configure postgres" in result.error
    assert result.code is ErrorCode.UNAVAILABLE and sql_env.db.opened == 0


async def test_the_password_never_appears_in_any_result_or_status(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    outputs = [
        await c.execute("list_connections", {}),
        await c.execute("health", {}),
        await c.execute("diagnose", {}),
        await c.execute("query", {"sql": "SELECT 1"}),
    ]
    text = repr([o.to_dict() for o in outputs]) + repr(c.status()) + repr(c.redacted_config())
    assert SECRET_PASSWORD not in text
    assert c.redacted_config()["password"] == "***"


async def test_a_driver_error_that_echoes_the_password_is_scrubbed(sql_env):
    sql_env.add("dev")
    sql_env.db.open_error = OperationalError(f"FATAL: connection failed password={SECRET_PASSWORD}")
    result = await sql_env.pg().execute("query", {"sql": "SELECT 1"})
    assert not result.ok and SECRET_PASSWORD not in result.error and "password=***" in result.error


# ── Locality and TLS ────────────────────────────────────────────────────────


async def test_a_remote_host_is_refused_unless_the_stored_connection_allows_it_with_tls(sql_env):
    sql_env.add("far", host="db.example.com")
    c = sql_env.pg()
    refused = await c.execute("query", {"sql": "SELECT 1", "connection": "far"})
    assert not refused.ok and "non-local" in refused.error and sql_env.db.opened == 0
    sql_env.add("far", host="db.example.com", allow_remote=True)  # no TLS requested
    no_tls = await c.execute("query", {"sql": "SELECT 1", "connection": "far"})
    assert not no_tls.ok and "TLS" in no_tls.error and sql_env.db.opened == 0
    sql_env.add("far", host="db.example.com", allow_remote=True, sslmode="verify-full")
    assert (await c.execute("query", {"sql": "SELECT 1", "connection": "far"})).ok


async def test_a_remote_connection_is_marked_in_listings_and_makes_writes_high_risk(sql_env):
    sql_env.add("far", host="db.example.com", allow_remote=True, sslmode="require")
    c = sql_env.pg()
    listing = await c.execute("list_connections", {})
    assert listing.data["connections"][0]["remote"] is True
    action = c.describe_actions(
        "execute", {"sql": "UPDATE t SET a=1 WHERE id=1", "connection": "far"}
    )[0]
    assert "remote server" in action.reason


# ── Driver availability ─────────────────────────────────────────────────────


async def test_a_missing_driver_is_unavailable_with_an_install_hint(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    c.driver_available = lambda: False  # type: ignore[method-assign]
    c.real_probe = True
    result = await c.connect()
    assert not result.ok and result.code is ErrorCode.UNAVAILABLE and "driver" in result.error
    assert c.status().state is ResourceState.UNAVAILABLE
    ok, reason = await c.probe_availability()
    assert not ok and "driver" in reason


# ── Lifecycle: connection, reuse, reconnect, release ────────────────────────


async def test_the_connection_is_opened_once_and_reused(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    for _ in range(4):
        assert (await c.execute("query", {"sql": "SELECT 1"})).ok
    assert sql_env.db.opened == 1 and c.status().state is ResourceState.CONNECTED


async def test_connect_failure_is_a_structured_error_and_retried_on_the_next_call(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    sql_env.db.open_error = OperationalError("could not connect to server: Connection refused")
    down = await c.execute("query", {"sql": "SELECT 1"})
    assert not down.ok and down.code is ErrorCode.CONNECTION_FAILED
    assert c.status().state is ResourceState.ERROR
    sql_env.db.open_error = None
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok  # recovers without a restart
    assert c.status().state is ResourceState.CONNECTED


async def test_a_dropped_connection_is_reopened_and_an_idempotent_read_is_retried(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok
    sql_env.db.connections[0].dead = True  # the server restarted
    again = await c.execute("query", {"sql": "SELECT 2"})
    assert again.ok and sql_env.db.opened == 2
    assert sql_env.db.connections[0].closed  # the dead one was released, not leaked


async def test_a_write_is_never_retried_after_the_connection_drops(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok
    sql_env.db.connections[0].dead = True
    result = await c.execute("execute", {"sql": "UPDATE t SET a = 1 WHERE id = 1"})
    assert not result.ok and result.code is ErrorCode.CONNECTION_FAILED
    assert "Outcome unknown" in result.error
    assert sql_env.db.opened == 1  # no silent second attempt
    assert [s for s in sql_env.db.user_sql() if s.startswith("UPDATE")] == []


async def test_disconnect_closes_every_connection(sql_env):
    sql_env.add("dev")
    sql_env.add("staging")
    c = sql_env.pg()
    await c.execute("query", {"sql": "SELECT 1", "connection": "dev"})
    await c.execute("query", {"sql": "SELECT 1", "connection": "staging"})
    assert sql_env.db.live == 2
    assert (await c.disconnect()).ok
    assert sql_env.db.live == 0 and c.status().state is ResourceState.DISCONNECTED


async def test_cursors_are_closed_after_every_statement(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    await c.execute("query", {"sql": "SELECT 1"})
    await c.execute("execute", {"sql": "UPDATE t SET a = 1 WHERE id = 1"})
    assert all(cur.closed for conn in sql_env.db.connections for cur in conn.cursors)


async def test_concurrent_calls_share_one_connection_and_never_interleave(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    results = await asyncio.gather(*[c.execute("query", {"sql": f"SELECT {i}"}) for i in range(8)])
    assert all(r.ok for r in results) and sql_env.db.opened == 1


# ── Health and diagnosis ────────────────────────────────────────────────────


async def test_health_reports_latency_and_connection(sql_env):
    sql_env.add("dev")
    result = await sql_env.pg().execute("health", {})
    assert result.ok and result.data["ok"] is True and "latency_ms" in result.data
    assert result.data["connection"] == "dev"


class _Writer:
    def close(self):
        pass


def _reachable(monkeypatch, ok=True):
    async def fake_open(host, port):
        if not ok:
            raise OSError("refused")
        return object(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", fake_open)


async def test_diagnose_walks_the_path_and_stops_where_it_fails(sql_env, monkeypatch):
    _reachable(monkeypatch)
    sql_env.add("dev")
    c = sql_env.pg()
    good = (await c.execute("diagnose", {})).data
    assert good["healthy"] and [s["step"] for s in good["steps"]][:4] == [
        "driver",
        "configuration",
        "validation",
        "policy",
    ]
    sql_env.add("dev", host="db.example.com")
    blocked = (await sql_env.pg().execute("diagnose", {})).data
    last = blocked["steps"][-1]
    assert not blocked["healthy"] and last["step"] == "policy" and "hint" in last
    sql_env.add("dev")
    sql_env.db.open_error = DriverError("password authentication failed for user", "28P01")
    login = (await sql_env.pg().execute("diagnose", {})).data
    assert not login["healthy"] and login["steps"][-1]["step"] == "login"
    assert login["steps"][-1]["hint"]


async def test_diagnose_reports_an_unreachable_server(sql_env, monkeypatch):
    _reachable(monkeypatch, ok=False)
    sql_env.add("dev")
    data = (await sql_env.pg().execute("diagnose", {})).data
    assert not data["healthy"] and data["steps"][-1]["step"] == "reachable"
    assert "listening" in data["steps"][-1]["hint"] and sql_env.db.opened == 0


async def test_diagnose_without_configuration_says_so(sql_env):
    data = (await sql_env.pg().execute("diagnose", {})).data
    assert not data["healthy"] and data["steps"][-1]["step"] == "configuration"


# ── Error normalization ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (DriverError("password authentication failed", "28P01"), ErrorCode.AUTHENTICATION_FAILED),
        (DriverError("permission denied for table t", "42501"), ErrorCode.AUTHORIZATION_DENIED),
        (DriverError('relation "t" does not exist', "42P01"), ErrorCode.NOT_FOUND),
        (DriverError("syntax error at or near", "42601"), ErrorCode.VALIDATION_FAILED),
        (DriverError("canceling statement", "57014"), ErrorCode.TIMEOUT),
        (DriverError("duplicate key", "23505"), ErrorCode.CONFLICT),
        (DriverError("deadlock detected", "40P01"), ErrorCode.CONFLICT),
        (DriverError("read-only transaction", "25006"), ErrorCode.AUTHORIZATION_DENIED),
        (DriverError("connection lost", "08006"), ErrorCode.CONNECTION_FAILED),
        (OperationalError("server closed the connection"), ErrorCode.CONNECTION_FAILED),
        (TimeoutError(), ErrorCode.TIMEOUT),
        (ConnectionResetError("reset"), ErrorCode.CONNECTION_FAILED),
        (ValueError("odd"), ErrorCode.ADAPTER_FAILURE),
    ],
)
def test_driver_errors_are_normalized(exc, code):
    assert classify_db_error(exc).code is code


def test_mysql_errnos_are_normalized():
    class MyError(Exception):
        pass

    for errno, code in [
        (1045, ErrorCode.AUTHENTICATION_FAILED),
        (1146, ErrorCode.NOT_FOUND),
        (1064, ErrorCode.VALIDATION_FAILED),
        (1062, ErrorCode.CONFLICT),
        (2013, ErrorCode.CONNECTION_FAILED),
        (3024, ErrorCode.TIMEOUT),
    ]:
        assert classify_db_error(MyError(errno, "message")).code is code


def test_a_mysql_error_with_a_generic_sqlstate_is_still_classified_by_its_errno():
    """Regression, found against a live MySQL 8.0.46: PyMySQL >= 1.2 also sets ``sqlstate``
    (``HY000`` for a statement timeout). That must not shadow the integer error number, or a
    timeout is reported as a lost connection and the model reconnects for nothing."""

    class MyOperationalError(Exception):  # mirrors pymysql.err.OperationalError
        sqlstate = "HY000"

    timeout = classify_db_error(
        MyOperationalError(3024, "Query execution was interrupted, maximum statement execution time exceeded")
    )
    assert timeout.code is ErrorCode.TIMEOUT and not timeout.connection_lost
    for errno, code in [
        (1045, ErrorCode.AUTHENTICATION_FAILED),
        (1062, ErrorCode.CONFLICT),
        (1205, ErrorCode.CONFLICT),
        (1792, ErrorCode.AUTHORIZATION_DENIED),
    ]:
        assert classify_db_error(MyOperationalError(errno, "m")).code is code
    assert classify_db_error(MyOperationalError(2013, "lost")).connection_lost


def test_only_the_primary_message_is_kept_so_row_values_do_not_leak():
    err = classify_db_error(
        DriverError(
            "duplicate key value violates unique constraint\nDETAIL: Key (email)=(a@b.c) already exists."
        )
    )
    assert "a@b.c" not in err.message and "duplicate key" in err.message


async def test_health_does_not_disturb_an_open_transaction(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    tid = (await c.execute("transaction_begin", {})).data["transaction_id"]
    health = await c.execute("health", {})
    assert health.ok and health.data["transaction_open"] == tid
    assert sql_env.db.executed == []  # no statement was sent on the busy connection
    assert (await c.execute("transaction_rollback", {"transaction_id": tid})).ok


async def test_a_configured_but_uncredentialed_connection_is_listed_as_unconfigured(sql_env):
    c = sql_env.pg(sql_env.settings(connections={"ghost": {"environment": "staging"}}))
    listing = await c.execute("list_connections", {})
    (entry,) = listing.data["connections"]
    assert entry["name"] == "ghost" and entry["configured"] is False
    assert "/resource configure postgres ghost" in entry["problem"]


def test_driver_errors_are_judged_by_their_whole_class_hierarchy():
    # psycopg 3 raises ConnectionTimeout, a subclass of OperationalError (observed against
    # the real driver on a closed port).
    class ConnectionTimeout(OperationalError):
        pass

    err = classify_db_error(ConnectionTimeout("connection timeout expired"))
    assert err.code is ErrorCode.CONNECTION_FAILED and err.connection_lost and err.hint


async def test_configuration_cannot_relabel_a_stored_production_connection(sql_env):
    sql_env.add("prod", environment="production")
    c = sql_env.pg(sql_env.settings(connections={"prod": {"environment": "development"}}))
    profile, _ = c._profile_for("prod")
    assert profile.environment == "production" and profile.production
    sql_env.add("plain")  # an unlabelled stored connection may be given a label
    labelled = sql_env.pg(sql_env.settings(connections={"plain": {"environment": "staging"}}))
    assert labelled._profile_for("plain")[0].environment == "staging"


async def test_an_untrusted_workspace_cannot_alias_another_stored_credential(sql_env):
    sql_env.add("prod", database="real")
    settings = sql_env.settings(connections={"innocent": {"credential": "prod"}})
    trusted = sql_env.pg(settings, workspace_trusted=True)
    assert (await trusted.execute("query", {"sql": "SELECT 1", "connection": "innocent"})).ok
    assert sql_env.db.configs[-1]["database"] == "real"
    untrusted = sql_env.pg(settings, workspace_trusted=False)
    refused = await untrusted.execute("query", {"sql": "SELECT 1", "connection": "innocent"})
    assert not refused.ok and refused.code is ErrorCode.UNAVAILABLE  # no credential of that name
    assert len(sql_env.db.configs) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"port": "abc"},
        {"port": 0},
        {"port": 70000},
        {"port": True},
        {"host": "local host"},
        {"host": "a;b"},
        {"host": "x\x00y"},
        {"host": ["localhost"]},
        {"host": "h" * 300},
        {"database": "d" * 200},
        {"username": 5},
        {"sslmode": "sometimes"},
    ],
)
async def test_a_malformed_stored_connection_is_a_clear_validation_error(sql_env, bad):
    sql_env.add("dev", **bad)
    c = sql_env.pg()
    result = await c.execute("query", {"sql": "SELECT 1"})
    assert not result.ok and result.code is ErrorCode.VALIDATION_FAILED
    assert "/resource configure postgres dev" in result.hint
    assert sql_env.db.opened == 0 and c.status().state is ResourceState.ERROR
    assert SECRET_PASSWORD not in result.error


@pytest.mark.parametrize(
    "good",
    [
        {"port": "5433"},
        {"host": "/var/run/postgresql"},
        {"host": "::1"},
        {"sslmode": "verify-full"},
        {},
    ],
)
async def test_ordinary_stored_connections_pass_validation(sql_env, good):
    sql_env.add("dev", **good)
    result = await sql_env.pg().execute("query", {"sql": "SELECT 1"})
    assert (
        result.ok or result.code is ErrorCode.AUTHORIZATION_DENIED
    )  # a TLS-less remote is policy, not validity
    assert result.code is not ErrorCode.VALIDATION_FAILED


async def test_diagnose_reports_a_malformed_connection_at_the_validation_step(sql_env):
    sql_env.add("dev", port="abc")
    data = (await sql_env.pg().execute("diagnose", {})).data
    assert not data["healthy"] and data["steps"][-1]["step"] == "validation"
    assert "number" in data["steps"][-1]["detail"]


async def test_a_connection_released_between_readiness_and_use_is_reopened(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    assert (await c.execute("query", {"sql": "SELECT 1"})).ok
    session = c._sessions["dev"]
    real_ready = c._ready

    async def ready_then_lose_it(name):
        got = await real_ready(name)
        await c._drop(session, rollback=False)  # another task released it in the meantime
        return got

    c._ready = ready_then_lose_it  # type: ignore[method-assign]
    result = await c.execute("query", {"sql": "SELECT 2"})
    assert result.ok and sql_env.db.opened == 2 and sql_env.db.live == 1
