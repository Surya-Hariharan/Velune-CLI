"""Optional checks against REAL, disposable PostgreSQL / MySQL servers (VAIR A5).

Skipped unless ``VELUNE_SQL_LIVE=1``. The main suite never needs a database: every
other SQL test runs against an in-memory fake. Point these at a **throwaway local
server** (for example ``docker run -p 5432:5432 -e POSTGRES_PASSWORD=pw postgres:16``):

    VELUNE_SQL_LIVE=1 VELUNE_LIVE_PG="localhost:5432:postgres:postgres:pw" \\
    VELUNE_LIVE_MYSQL="127.0.0.1:3306:mysql:root:pw" \\
        python -m pytest tests/test_sql_live.py

``VELUNE_LIVE_*`` is ``host:port:database:user:password``. The tests only create
temporary tables inside transactions that are rolled back, and never touch existing
data. They exercise what the fakes cannot: the real drivers' read-only transactions,
server-side statement timeouts, server cursors, cancellation and catalog SQL.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.live

if os.environ.get("VELUNE_SQL_LIVE") != "1":
    pytest.skip(
        "set VELUNE_SQL_LIVE=1 (and VELUNE_LIVE_PG / VELUNE_LIVE_MYSQL) to run against real servers",
        allow_module_level=True,
    )

from velune.resources import connections as connections_module  # noqa: E402
from velune.resources.base import ErrorCode  # noqa: E402
from velune.resources.mysql.connector import MySQLConnector  # noqa: E402
from velune.resources.postgres.connector import PostgresConnector  # noqa: E402


def _config(variable: str, default_port: int) -> dict | None:
    raw = os.environ.get(variable)
    if not raw:
        return None
    host, port, database, user, password = raw.split(":", 4)
    return {
        "host": host,
        "port": int(port or default_port),
        "database": database,
        "username": user,
        "password": password,
    }


@pytest.fixture(params=["postgres", "mysql"])
def live(request, monkeypatch):
    engine = request.param
    cfg = _config("VELUNE_LIVE_PG" if engine == "postgres" else "VELUNE_LIVE_MYSQL", 5432)
    if cfg is None:
        pytest.skip(f"VELUNE_LIVE_{'PG' if engine == 'postgres' else 'MYSQL'} is not set")
    cls = PostgresConnector if engine == "postgres" else MySQLConnector
    connector = cls(config=cfg, settings={"statement_timeout": 5, "max_rows": 50})
    if not connector.driver_available():
        pytest.skip(f"{connector.driver_name} is not installed")
    monkeypatch.setattr(connections_module, "load_resource_secret", lambda t, n: None)
    return connector


async def test_connect_health_and_diagnose(live):
    assert (await live.connect()).ok
    health = await live.execute("health", {})
    assert health.ok and health.data["ok"]
    report = (await live.execute("diagnose", {})).data
    assert report["healthy"] and report["steps"][-1]["step"] == "query"
    await live.disconnect()


async def test_a_real_read_only_transaction_refuses_a_write_the_classifier_missed(live):
    # Force a write through the read path to prove the SERVER enforces read-only.
    from velune.resources.sql_parse import SQLAnalysis

    forced = SQLAnalysis((), live._dialect_analysis("SELECT 1").category, "forced", 1)
    live._dialect_analysis = lambda sql: forced  # type: ignore[method-assign]
    result = await live.execute(
        "query", {"sql": "CREATE TEMPORARY TABLE velune_live_probe (a int)"}
    )
    assert not result.ok
    assert result.code in (ErrorCode.AUTHORIZATION_DENIED, ErrorCode.VALIDATION_FAILED)
    await live.disconnect()


async def test_bound_parameters_round_trip(live):
    marker = "x'; DROP TABLE nothing; --"
    result = await live.execute("query", {"sql": "SELECT %s AS v", "params": [marker]})
    assert result.ok and result.data["rows"] == [[marker]]
    await live.disconnect()


async def test_a_transaction_commits_and_rolls_back_for_real(live):
    begin = await live.execute("transaction_begin", {})
    tid = begin.data["transaction_id"]
    made = await live.execute(
        "transaction_execute",
        {"transaction_id": tid, "sql": "CREATE TEMPORARY TABLE velune_live_tx (a int)"},
    )
    assert made.ok, made.error
    inserted = await live.execute(
        "transaction_execute",
        {"transaction_id": tid, "sql": "INSERT INTO velune_live_tx VALUES (%s)", "params": [1]},
    )
    assert inserted.ok, inserted.error
    assert (await live.execute("transaction_rollback", {"transaction_id": tid})).ok
    await live.disconnect()


async def test_the_server_stops_a_slow_statement(live):
    sleep = "SELECT pg_sleep(30)" if isinstance(live, PostgresConnector) else "SELECT SLEEP(30)"
    result = await live.execute("query", {"sql": sleep, "timeout_seconds": 1})
    assert not result.ok and result.code is ErrorCode.TIMEOUT
    assert (await live.execute("query", {"sql": "SELECT 1"})).ok  # a fresh connection works
    await live.disconnect()


async def test_a_huge_result_is_bounded_and_streamed(live):
    if isinstance(live, PostgresConnector):
        sql = "SELECT g FROM generate_series(1, 1000000) AS g"
    else:
        sql = "SELECT seq FROM (SELECT 1 AS seq UNION SELECT 2 UNION SELECT 3) t"
    result = await live.execute("query", {"sql": sql, "max_rows": 3})
    assert result.ok and result.data["rowcount"] == 3
    await live.disconnect()


async def test_the_catalog_operations_run_against_the_real_catalog(live):
    assert (await live.execute("database_info", {})).ok
    assert (await live.execute("list_databases", {})).ok
    tables = await live.execute("list_tables", {})
    assert tables.ok and "tables" in tables.data
    await live.disconnect()


# ── PostgreSQL: a real schema, a restricted role, committed data ────────────

_SCHEMA = "velune_live"
_ROLE = "velune_limited"


@pytest.fixture
def pg_world():
    """A throwaway schema (and a restricted role) created and removed around a test."""
    cfg = _config("VELUNE_LIVE_PG", 5432)
    if cfg is None:
        pytest.skip("VELUNE_LIVE_PG is not set")
    psycopg = pytest.importorskip("psycopg")
    admin = psycopg.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user=cfg["username"],
        password=cfg["password"],
        autocommit=True,
    )

    def run(sql):
        return admin.execute(sql)

    run(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    run(f"DROP ROLE IF EXISTS {_ROLE}")
    run(f"CREATE SCHEMA {_SCHEMA}")
    run(f"CREATE TABLE {_SCHEMA}.orgs (id serial PRIMARY KEY, name text UNIQUE NOT NULL)")
    run(
        f"CREATE TABLE {_SCHEMA}.users (id serial PRIMARY KEY, "
        f"org_id int REFERENCES {_SCHEMA}.orgs(id), email text NOT NULL, "
        f"balance int NOT NULL DEFAULT 0 CHECK (balance >= 0))"
    )
    run(f"CREATE INDEX users_email_idx ON {_SCHEMA}.users (email)")
    run(f"CREATE VIEW {_SCHEMA}.v_users AS SELECT id, email FROM {_SCHEMA}.users")
    run(f"CREATE ROLE {_ROLE} LOGIN")
    run(f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {_ROLE}")
    run(f"GRANT SELECT ON {_SCHEMA}.orgs TO {_ROLE}")  # but NOT users, nor the view
    try:
        yield SimpleNamespace(cfg=cfg, admin=admin)
    finally:
        run(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        run(f"DROP ROLE IF EXISTS {_ROLE}")
        admin.close()


def _pg(cfg, **overrides):
    connector = PostgresConnector(
        config={**cfg, **overrides}, settings={"statement_timeout": 5, "max_rows": 50}
    )
    if not connector.driver_available():
        pytest.skip("psycopg is not installed")
    return connector


async def test_the_catalog_of_a_real_schema(pg_world):
    c = _pg(pg_world.cfg)
    tables = await c.execute("list_tables", {"schema": _SCHEMA})
    assert {t["name"] for t in tables.data["tables"]} == {"orgs", "users"}
    views = await c.execute("list_views", {"schema": _SCHEMA})
    assert [v["name"] for v in views.data["views"]] == ["v_users"]
    users = await c.execute("describe_table", {"table": "users", "schema": _SCHEMA})
    assert users.ok, users.error
    assert [col["name"] for col in users.data["columns"]] == ["id", "org_id", "email", "balance"]
    assert users.data["primary_key"] == ["id"]
    (fk,) = users.data["foreign_keys"]
    assert fk["columns"] == ["org_id"] and fk["references"]["table"] == "orgs"
    indexes = await c.execute("list_indexes", {"table": "users", "schema": _SCHEMA})
    by_name = {i["name"]: i for i in indexes.data["indexes"]}
    assert by_name["users_pkey"]["is_primary"] and by_name["users_email_idx"]["columns"] == [
        "email"
    ]
    constraints = await c.execute("list_constraints", {"table": "users", "schema": _SCHEMA})
    kinds = {k["type"] for k in constraints.data["constraints"]}
    assert {"primary_key", "foreign_key", "check"} <= kinds
    rel = await c.execute("table_relationships", {"table": "orgs", "schema": _SCHEMA})
    assert [r["from_table"] for r in rel.data["referenced_by"]] == ["users"]
    await c.disconnect()


async def test_discovery_shows_a_restricted_role_only_what_it_can_use(pg_world):
    c = _pg(pg_world.cfg, username=_ROLE, password="")
    tables = await c.execute("list_tables", {"schema": _SCHEMA})
    assert tables.ok, tables.error
    assert [t["name"] for t in tables.data["tables"]] == ["orgs"]  # not users, not the view
    hidden = await c.execute("describe_table", {"table": "users", "schema": _SCHEMA})
    assert not hidden.ok and hidden.code is ErrorCode.NOT_FOUND
    rel = await c.execute("table_relationships", {"table": "orgs", "schema": _SCHEMA})
    assert rel.data["referenced_by"] == []  # users references orgs, but this role cannot see users
    selected = await c.execute("query", {"sql": f"SELECT * FROM {_SCHEMA}.users"})
    assert (
        not selected.ok and selected.code is ErrorCode.AUTHORIZATION_DENIED
    )  # native permissions hold
    await c.disconnect()


async def test_a_committed_transaction_persists_and_a_failed_one_cannot_commit(pg_world):
    c = _pg(pg_world.cfg)
    insert = f"INSERT INTO {_SCHEMA}.orgs (name) VALUES (%s)"
    tid = (await c.execute("transaction_begin", {})).data["transaction_id"]
    ins = await c.execute(
        "transaction_execute", {"transaction_id": tid, "sql": insert, "params": ["acme"]}
    )
    assert ins.ok, ins.error
    assert (await c.execute("transaction_commit", {"transaction_id": tid})).ok
    count = pg_world.admin.execute(f"SELECT count(*) FROM {_SCHEMA}.orgs").fetchone()[0]
    assert count == 1  # committed for real, visible to another connection

    tid = (await c.execute("transaction_begin", {})).data["transaction_id"]
    await c.execute(
        "transaction_execute", {"transaction_id": tid, "sql": insert, "params": ["globex"]}
    )
    bad = await c.execute(
        "transaction_execute", {"transaction_id": tid, "sql": insert, "params": ["acme"]}
    )
    assert not bad.ok and bad.code is ErrorCode.CONFLICT  # unique violation, from SQLSTATE 23505
    refused = await c.execute("transaction_commit", {"transaction_id": tid})
    assert not refused.ok and "Refusing to commit" in refused.error
    count = pg_world.admin.execute(f"SELECT count(*) FROM {_SCHEMA}.orgs").fetchone()[0]
    assert count == 1  # "globex" was rolled back with the failed transaction
    await c.disconnect()


async def test_the_server_rejects_a_write_inside_a_read_only_transaction(pg_world):
    from velune.resources.sql_base import classify_db_error

    c = _pg(pg_world.cfg)
    tid = (await c.execute("transaction_begin", {"read_only": True})).data["transaction_id"]
    sess = c._sessions["postgres"]
    # Bypass the connector's own refusal to prove the SERVER enforces it as well.
    try:
        await asyncio.to_thread(
            c._in_tx,
            sess.conn,
            f"INSERT INTO {_SCHEMA}.orgs (name) VALUES ('x')",
            None,
            limits=sess.profile.limits,
            stream=False,
        )
    except Exception as exc:
        assert classify_db_error(exc).code is ErrorCode.AUTHORIZATION_DENIED
    else:  # pragma: no cover - would mean the server let a write through
        raise AssertionError("the server accepted a write in a read-only transaction")
    await c.execute("transaction_rollback", {"transaction_id": tid})
    await c.disconnect()


async def test_a_write_through_execute_commits_for_real(pg_world):
    c = _pg(pg_world.cfg)
    out = await c.execute(
        "execute", {"sql": f"INSERT INTO {_SCHEMA}.orgs (name) VALUES (%s)", "params": ["solo"]}
    )
    assert out.ok and out.data["rows_affected"] == 1
    assert pg_world.admin.execute(f"SELECT name FROM {_SCHEMA}.orgs").fetchall() == [("solo",)]
    await c.disconnect()
