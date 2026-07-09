"""Tests for the Resource Connector Framework.

Covers the shared framework (value types, SQL classification, encrypted
secrets, discovery), the manager's authorization gating, and each of the four
production connectors (Docker, PostgreSQL, MySQL, Supabase) — including the
"never crash when the backing service is absent" guarantee.
"""

from __future__ import annotations

from typing import Any

import pytest

from velune.resources.base import (
    AuthorizationRequest,
    DiscoveryHint,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.manager import (
    ResourceManager,
    approve_read_only,
    build_default_manager,
)
from velune.resources.sql_safety import classify_sql, is_read_only

# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeConnector(ResourceConnector):
    """Minimal connector for exercising the manager without real services."""

    resource_id = "fake"
    display_name = "Fake"

    def __init__(self, name: str | None = None) -> None:
        super().__init__(name)
        self.executed: list[tuple[str, dict]] = []
        self._connected = False

    async def connect(self) -> ResourceResult:
        self._connected = True
        return ResourceResult.success(action="connect")

    async def disconnect(self) -> ResourceResult:
        self._connected = False
        return ResourceResult.success(action="disconnect")

    def status(self) -> ResourceStatus:
        state = ResourceState.CONNECTED if self._connected else ResourceState.DISCONNECTED
        return ResourceStatus(self.resource_id, self.display_name, state)

    async def discover(self) -> list[DiscoveryHint]:
        return [DiscoveryHint("fake", "Fake", detail="here", source="test")]

    def capabilities(self) -> list[ResourceCapability]:
        return [
            ResourceCapability("read_it", ResourcePermission.READ, "read"),
            ResourceCapability("write_it", ResourcePermission.WRITE, "write"),
            ResourceCapability("drop_it", ResourcePermission.ADMIN, "drop", destructive=True),
        ]

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        self.executed.append((action, params or {}))
        return ResourceResult.success(action=action, data="ran")


# ── Value types ──────────────────────────────────────────────────────────────


def test_result_factories():
    ok = ResourceResult.success("a", data=1)
    assert ok.ok and ok.data == 1 and not ok.denied
    bad = ResourceResult.failure("boom", action="a")
    assert not bad.ok and bad.error == "boom"
    denied = ResourceResult.denied_result("a", "no")
    assert not denied.ok and denied.denied


def test_capability_lookup():
    c = FakeConnector()
    assert c.capability_for("read_it").permission is ResourcePermission.READ
    assert c.capability_for("nope") is None


# ── SQL classification ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql,expected,read_only",
    [
        ("SELECT * FROM users", ResourcePermission.READ, True),
        ("  select 1  ", ResourcePermission.READ, True),
        ("EXPLAIN SELECT 1", ResourcePermission.READ, True),
        ("SHOW TABLES", ResourcePermission.READ, True),
        ("WITH x AS (SELECT 1) SELECT * FROM x", ResourcePermission.READ, True),
        ("INSERT INTO t VALUES (1)", ResourcePermission.WRITE, False),
        ("UPDATE t SET x = 1", ResourcePermission.WRITE, False),
        ("DELETE FROM t", ResourcePermission.WRITE, False),
        ("CREATE TABLE t (id int)", ResourcePermission.WRITE, False),
        ("ALTER TABLE t ADD c int", ResourcePermission.WRITE, False),
        ("DROP TABLE t", ResourcePermission.ADMIN, False),
        ("TRUNCATE t", ResourcePermission.ADMIN, False),
    ],
)
def test_classify_sql(sql, expected, read_only):
    verdict = classify_sql(sql)
    assert verdict.permission is expected
    assert verdict.read_only is read_only


def test_classify_sql_multistatement_escalates():
    # A hidden DROP behind a SELECT must not ride in as read-only.
    verdict = classify_sql("SELECT 1; DROP TABLE t")
    assert verdict.permission is ResourcePermission.ADMIN
    assert not verdict.read_only


def test_classify_sql_comment_stripping():
    assert is_read_only("/* comment */ SELECT 1")
    assert is_read_only("SELECT 1 -- DROP TABLE t")  # DROP is in a line comment
    assert not is_read_only("-- just a comment")  # comment-only → not read-only


def test_classify_sql_unknown_escalates():
    assert classify_sql("GIBBERISH foo").permission is ResourcePermission.WRITE


# ── Secrets (encrypted keystore reuse) ───────────────────────────────────────


@pytest.fixture
def fake_keystore(monkeypatch):
    """In-memory stand-in for the encrypted keystore functions."""
    store: dict[str, str] = {}
    monkeypatch.setattr("velune.resources.secrets.save_key", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr("velune.resources.secrets.get_key", lambda k: store.get(k))
    monkeypatch.setattr("velune.resources.secrets.delete_key", lambda k: store.pop(k, None))
    return store


def test_secret_roundtrip(fake_keystore):
    from velune.resources.secrets import (
        delete_resource_secret,
        load_resource_secret,
        save_resource_secret,
    )

    cfg = {"host": "localhost", "password": "s3cret"}
    save_resource_secret("postgres", "default", cfg)
    # Stored under a namespaced id; the raw value is serialized, not plaintext dict.
    assert any(k.startswith("resource:postgres:") for k in fake_keystore)
    assert load_resource_secret("postgres", "default") == cfg
    delete_resource_secret("postgres", "default")
    assert load_resource_secret("postgres", "default") is None


def test_redact_config():
    from velune.resources.secrets import redact_config

    red = redact_config({"host": "localhost", "password": "abc", "service_role_key": "xyz"})
    assert red["host"] == "localhost"
    assert red["password"] == "***"
    assert red["service_role_key"] == "***"


# ── Discovery ────────────────────────────────────────────────────────────────


def test_scan_workspace_none_and_missing(tmp_path):
    from velune.resources.discovery import scan_workspace

    assert scan_workspace(None) == []
    assert scan_workspace(tmp_path / "nope") == []


def test_scan_workspace_compose_and_supabase(tmp_path):
    from velune.resources.discovery import scan_workspace

    (tmp_path / "docker-compose.yml").write_text("services: {}")
    (tmp_path / "supabase").mkdir()
    hints = scan_workspace(tmp_path)
    ids = {h.resource_id for h in hints}
    assert "docker" in ids
    assert "supabase" in ids


def test_scan_workspace_env_db_url_drops_password(tmp_path):
    from velune.resources.discovery import scan_workspace

    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgres://user:supersecret@localhost:5432/mydb\n"
        "SUPABASE_URL=https://abc.supabase.co\n"
        "SUPABASE_ANON_KEY=anon123\n"
    )
    hints = scan_workspace(tmp_path)
    pg = next(h for h in hints if h.resource_id == "postgres")
    assert pg.suggested["host"] == "localhost"
    assert pg.suggested["port"] == 5432
    assert pg.suggested["database"] == "mydb"
    # The password must never appear anywhere in a hint.
    blob = repr(hints)
    assert "supersecret" not in blob
    assert "anon123" not in blob


# ── Manager: registration & lifecycle ────────────────────────────────────────


def test_default_manager_registers_four():
    m = build_default_manager()
    assert set(m.list_ids()) == {"docker", "postgres", "mysql", "supabase"}


def test_manager_respects_disabled_config():
    m = build_default_manager(config={"docker": {"enabled": False}})
    assert "docker" not in m.list_ids()
    assert m.get("docker") is None
    assert m.status("docker") is None


def test_manager_auto_connect_targets():
    m = build_default_manager(config={"supabase": {"auto_connect": True}, "docker": {}})
    assert m.auto_connect_targets() == ["supabase"]


async def test_manager_connect_unknown_resource():
    m = ResourceManager()
    result = await m.connect("ghost")
    assert not result.ok and "No connector" in result.error


async def test_manager_discover_aggregates(tmp_path):
    m = ResourceManager(workspace=tmp_path)
    m.register("fake", FakeConnector)
    (tmp_path / "compose.yaml").write_text("x")
    hints = await m.discover()
    ids = {h.resource_id for h in hints}
    assert "fake" in ids  # from connector.discover()
    assert "docker" in ids  # from workspace scan


# ── Manager: authorization gating ────────────────────────────────────────────


async def test_read_action_auto_approved():
    m = ResourceManager()
    m.register("fake", FakeConnector)
    result = await m.execute("fake", "read_it", {})
    assert result.ok and result.data == "ran"


async def test_write_action_denied_by_default():
    m = ResourceManager()  # default approver = read-only
    m.register("fake", FakeConnector)
    result = await m.execute("fake", "write_it", {})
    assert not result.ok and result.denied


async def test_write_action_allowed_with_approver():
    seen: list[AuthorizationRequest] = []

    async def yes(req: AuthorizationRequest) -> bool:
        seen.append(req)
        return True

    m = ResourceManager(approver=yes)
    m.register("fake", FakeConnector)
    result = await m.execute("fake", "write_it", {})
    assert result.ok
    assert seen[0].permission is ResourcePermission.WRITE


async def test_destructive_flag_reaches_approver():
    captured: list[AuthorizationRequest] = []

    async def approver(req: AuthorizationRequest) -> bool:
        captured.append(req)
        return False

    m = ResourceManager(approver=approver)
    m.register("fake", FakeConnector)
    await m.execute("fake", "drop_it", {})
    assert captured[0].destructive is True
    assert captured[0].permission is ResourcePermission.ADMIN


async def test_approver_crash_fails_closed():
    async def boom(req: AuthorizationRequest) -> bool:
        raise RuntimeError("approver exploded")

    m = ResourceManager(approver=boom)
    m.register("fake", FakeConnector)
    result = await m.execute("fake", "write_it", {})
    assert not result.ok and result.denied


async def test_unknown_action_structured_error():
    m = ResourceManager()
    m.register("fake", FakeConnector)
    result = await m.execute("fake", "nonexistent", {})
    assert not result.ok and "Unknown action" in result.error


async def test_per_call_approver_override():
    async def yes(req: AuthorizationRequest) -> bool:
        return True

    m = ResourceManager()  # default denies writes
    m.register("fake", FakeConnector)
    result = await m.execute("fake", "write_it", {}, approver=yes)
    assert result.ok


# ── Docker connector ─────────────────────────────────────────────────────────


async def test_docker_not_installed_graceful(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: None))
    c = DockerConnector()
    result = await c.connect()
    assert not result.ok
    assert c.status().state is ResourceState.UNAVAILABLE
    assert await c.discover() == []  # no crash, no hint


def test_docker_capabilities_tiers():
    from velune.resources.docker.connector import DockerConnector

    caps = {c.action: c for c in DockerConnector().capabilities()}
    assert caps["ps"].permission is ResourcePermission.READ
    assert caps["stop"].permission is ResourcePermission.WRITE
    assert caps["compose_up"].permission is ResourcePermission.EXECUTE
    assert caps["compose_down"].destructive is True


async def test_docker_ps_parses_json(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    c._state = ResourceState.CONNECTED

    async def fake_run(args, timeout=20.0):
        return ResourceResult.success(
            data='{"ID":"abc","Names":"web"}\n{"ID":"def","Names":"db"}\n'
        )

    monkeypatch.setattr(c, "_run", fake_run)
    result = await c.execute("ps", {})
    assert result.ok
    assert [row["Names"] for row in result.data] == ["web", "db"]


async def test_docker_inspect_requires_target():
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    c._state = ResourceState.CONNECTED
    result = await c.execute("inspect", {})
    assert not result.ok and "requires" in result.error


# ── PostgreSQL connector ─────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def execute(self, sql):
        self._sql = sql

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, description=None, rows=None):
        self._description = description if description is not None else [("col",)]
        self._rows = rows if rows is not None else [[1]]

    def cursor(self):
        return _FakeCursor(self._description, self._rows)

    def close(self):
        pass


def test_postgres_local_host_validation():
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector()
    assert c.is_local_host("localhost")
    assert c.is_local_host("127.0.0.1")
    assert c.is_local_host("::1")
    assert c.is_local_host("/var/run/postgresql")
    assert c.is_local_host(None)
    assert not c.is_local_host("db.example.com")
    assert not c.is_local_host("10.0.0.5")


async def test_postgres_refuses_remote_host(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "db.example.com", "database": "x"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    result = await c.connect()
    assert not result.ok and "non-local" in result.error.lower()
    assert c.status().state is ResourceState.ERROR


async def test_postgres_missing_driver(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost"})
    monkeypatch.setattr(c, "driver_available", lambda: False)
    result = await c.connect()
    assert not result.ok and "driver" in result.error.lower()
    assert c.status().state is ResourceState.UNAVAILABLE


async def test_postgres_query_execution(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost", "database": "app"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    monkeypatch.setattr(
        c, "_open_connection", lambda cfg: _FakeConn([("id",), ("name",)], [[1, "a"]])
    )
    result = await c.execute("query", {"sql": "SELECT id, name FROM t"})
    assert result.ok
    assert result.data["columns"] == ["id", "name"]
    assert result.data["rows"] == [[1, "a"]]


def test_postgres_effective_permission_escalates():
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector()
    assert c.effective_permission("query", {"sql": "SELECT 1"})[0] is ResourcePermission.READ
    assert c.effective_permission("query", {"sql": "DROP TABLE t"})[0] is ResourcePermission.ADMIN
    assert (
        c.effective_permission("query", {"sql": "UPDATE t SET x=1"})[0] is ResourcePermission.WRITE
    )


async def test_postgres_describe_rejects_bad_identifier(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    monkeypatch.setattr(c, "_open_connection", lambda cfg: _FakeConn())
    result = await c.execute("describe_table", {"table": "users; DROP TABLE x"})
    assert not result.ok and "identifier" in result.error.lower()


async def test_postgres_error_sanitizes_password(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost", "password": "hunter2"})
    monkeypatch.setattr(c, "driver_available", lambda: True)

    def boom(cfg):
        raise RuntimeError("connection failed: password=hunter2 host=localhost")

    monkeypatch.setattr(c, "_open_connection", boom)
    result = await c.connect()
    assert not result.ok
    assert "hunter2" not in result.error
    assert "password=***" in result.error


# ── MySQL connector ──────────────────────────────────────────────────────────


async def test_mysql_query_and_describe(monkeypatch):
    from velune.resources.mysql.connector import MySQLConnector

    c = MySQLConnector(config={"host": "127.0.0.1"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    monkeypatch.setattr(c, "_open_connection", lambda cfg: _FakeConn([("Database",)], [["app"]]))
    result = await c.execute("list_databases", {})
    assert result.ok and result.data["rows"] == [["app"]]

    bad = await c.execute("describe_table", {"table": "a`b"})
    assert not bad.ok


def test_mysql_defaults():
    from velune.resources.mysql.connector import MySQLConnector

    c = MySQLConnector()
    assert c.default_port == 3306
    actions = {cap.action for cap in c.capabilities()}
    assert {"list_databases", "list_tables", "describe_table", "query"} <= actions


# ── Supabase connector ───────────────────────────────────────────────────────


def test_supabase_url_validation():
    from velune.resources.supabase.connector import SupabaseConnector

    assert SupabaseConnector._validate_url("https://abc.supabase.co") == "https://abc.supabase.co"
    assert SupabaseConnector._validate_url("http://abc.supabase.co") is None  # not https
    assert SupabaseConnector._validate_url("not a url") is None


async def test_supabase_no_config():
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config=None)
    result = await c.execute("verify", {})
    assert not result.ok


def test_supabase_service_role_escalates():
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector()
    assert c.effective_permission("query", {}) is None
    perm, _ = c.effective_permission("query", {"service_role": True})
    assert perm is ResourcePermission.WRITE


async def test_supabase_query_validates_table(monkeypatch):
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    result = await c.execute("query", {"table": "bad table; DROP"})
    assert not result.ok and "identifier" in result.error.lower()


async def test_supabase_list_tables_from_openapi(monkeypatch):
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    monkeypatch.setattr(c, "driver_available", lambda: True)

    async def fake_request(config, method, path, **kw):
        return ResourceResult.success(data={"definitions": {"users": {}, "posts": {}}})

    monkeypatch.setattr(c, "_request", fake_request)
    result = await c.execute("list_tables", {})
    assert result.ok
    assert set(result.data["tables"]) == {"users", "posts"}


async def test_supabase_rls_requires_service_role(monkeypatch):
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    result = await c.execute("rls_policies", {})
    assert not result.ok and "service-role" in result.error.lower()


# ── Default approver ─────────────────────────────────────────────────────────


async def test_default_approver_read_only():
    read_req = AuthorizationRequest("x", "X", "a", ResourcePermission.READ, False)
    write_req = AuthorizationRequest("x", "X", "a", ResourcePermission.WRITE, False)
    assert await approve_read_only(read_req) is True
    assert await approve_read_only(write_req) is False


# ── Manager: status, sessions, teardown, robustness ──────────────────────────


async def test_manager_all_status_and_sessions():
    m = ResourceManager()
    m.register("fake", FakeConnector)
    statuses = m.all_status()
    assert [s.resource_id for s in statuses] == ["fake"]
    assert m.active_sessions() == []
    await m.connect("fake")
    assert m.active_sessions() == ["fake"]
    await m.disconnect_all()
    assert m.active_sessions() == []


def test_manager_capabilities_and_status_unknown():
    m = ResourceManager()
    assert m.capabilities("ghost") == []
    assert m.status("ghost") is None


async def test_manager_connect_disconnect_delegate():
    m = ResourceManager()
    m.register("fake", FakeConnector)
    assert (await m.connect("fake")).ok
    assert (await m.disconnect("fake")).ok
    assert (await m.disconnect("ghost")).ok is False


async def test_manager_execute_connector_exception():
    class Boom(FakeConnector):
        async def execute(self, action, params=None):
            raise RuntimeError("kaboom")

    m = ResourceManager()
    m.register("fake", Boom)
    result = await m.execute("fake", "read_it", {})
    assert not result.ok and "kaboom" in result.error


def test_manager_factory_raising_returns_none():
    def bad_factory():
        raise RuntimeError("cannot build")

    m = ResourceManager()
    m.register("bad", bad_factory)
    assert m.get("bad") is None


def test_manager_status_of_raising_connector():
    class BadStatus(FakeConnector):
        def status(self):
            raise RuntimeError("no status")

    m = ResourceManager()
    m.register("fake", BadStatus)
    st = m.status("fake")
    assert st.state is ResourceState.ERROR


# ── Docker: connect success + all action handlers ────────────────────────────


def _docker_runner(script):
    """Build a fake `_run` returning queued results keyed by first arg."""

    async def fake_run(args, timeout=20.0):
        return script.get(args[0], ResourceResult.success(data=""))

    return fake_run


async def test_docker_connect_success(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: "docker"))

    async def fake_run(args, timeout=20.0):
        if args[0] == "version":
            return ResourceResult.success(data='{"Server":{"Version":"25.0.1"}}')
        return ResourceResult.success(data="ok")

    monkeypatch.setattr(c, "_run", fake_run)
    result = await c.connect()
    assert result.ok
    st = c.status()
    assert st.state is ResourceState.CONNECTED
    assert "25.0.1" in st.detail
    assert st.info["compose"] is True
    assert (await c.disconnect()).ok
    assert c.status().state is ResourceState.DISCONNECTED


async def test_docker_connect_daemon_down(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: "docker"))

    async def fake_run(args, timeout=20.0):
        return ResourceResult.failure("cannot connect")

    monkeypatch.setattr(c, "_run", fake_run)
    result = await c.connect()
    assert not result.ok
    assert c.status().state is ResourceState.ERROR


async def test_docker_discover_running_and_stopped(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: "docker"))

    async def running(args, timeout=20.0):
        return ResourceResult.success(data="25.0.1\n")

    monkeypatch.setattr(c, "_run", running)
    hints = await c.discover()
    assert hints and hints[0].source == "daemon"

    async def stopped(args, timeout=20.0):
        return ResourceResult.failure("daemon down")

    monkeypatch.setattr(c, "_run", stopped)
    hints = await c.discover()
    assert hints and "stopped" in hints[0].display_name.lower()


async def test_docker_all_read_and_write_actions(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    c._state = ResourceState.CONNECTED
    c._has_compose = True
    calls: list[list[str]] = []

    async def fake_run(args, timeout=20.0):
        calls.append(args)
        if "json" in " ".join(args):
            return ResourceResult.success(data='{"k":"v"}\n')
        return ResourceResult.success(data="done")

    monkeypatch.setattr(c, "_run", fake_run)

    assert (await c.execute("images", {})).ok
    assert (await c.execute("network_ls", {})).ok
    assert (await c.execute("volume_ls", {})).ok
    assert (await c.execute("logs", {"container": "web", "tail": 5})).ok
    inspect = await c.execute("inspect", {"container": "web"})
    assert inspect.ok
    assert (await c.execute("start", {"container": "web"})).ok
    assert (await c.execute("stop", {"container": "web"})).ok
    assert (await c.execute("restart", {"container": "web"})).ok
    assert (await c.execute("build", {"path": ".", "tag": "img:1"})).ok
    assert (await c.execute("compose_up", {"file": "compose.yaml"})).ok
    assert (await c.execute("compose_down", {"volumes": True})).ok
    # Missing-target validations.
    assert not (await c.execute("logs", {})).ok
    assert not (await c.execute("start", {})).ok
    assert not (await c.execute("stop", {})).ok
    assert not (await c.execute("restart", {})).ok
    # Unknown action.
    assert not (await c.execute("bogus", {})).ok


async def test_docker_compose_unavailable(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    c._state = ResourceState.CONNECTED
    c._has_compose = False
    result = await c.execute("compose_up", {})
    assert not result.ok and "compose" in result.error.lower()


def test_docker_authorization_preview():
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    assert "web" in c.authorization_preview("stop", {"container": "web"})


async def test_docker_run_missing_cli(monkeypatch):
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: None))
    result = await c._run(["ps"])
    assert not result.ok and "not found" in result.error.lower()


# ── SQL base: shared paths via Postgres ──────────────────────────────────────


async def test_sql_health_and_explain(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost", "database": "app"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    captured: list[str] = []

    def fake_open(cfg):
        return _FakeConn([("?column?",)], [[1]])

    monkeypatch.setattr(c, "_open_connection", fake_open)

    orig = c._execute_sql

    def spy(conn, sql):
        captured.append(sql)
        return orig(conn, sql)

    monkeypatch.setattr(c, "_execute_sql", spy)
    assert (await c.execute("health", {})).ok
    assert (await c.execute("explain", {"sql": "SELECT 1"})).ok
    assert any("SELECT 1" in s for s in captured)
    assert any(s.startswith("EXPLAIN") for s in captured)


async def test_sql_introspection_actions(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost", "database": "app"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    monkeypatch.setattr(c, "_open_connection", lambda cfg: _FakeConn([("x",)], [["y"]]))
    for action, params in [
        ("list_databases", {}),
        ("list_schemas", {}),
        ("list_tables", {}),
        ("list_tables", {"schema": "public"}),
        ("describe_table", {"table": "users", "schema": "public"}),
    ]:
        assert (await c.execute(action, params)).ok, action
    # Invalid schema on list_tables.
    assert not (await c.execute("list_tables", {"schema": "bad;schema"})).ok


async def test_sql_query_requires_sql(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    monkeypatch.setattr(c, "_open_connection", lambda cfg: _FakeConn())
    assert not (await c.execute("query", {"sql": "   "})).ok
    assert not (await c.execute("explain", {})).ok


def test_sql_detail_and_redacted_config(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(
        config={"host": "localhost", "port": 5432, "database": "app", "password": "p"}
    )
    c._info = {"host": "localhost", "port": 5432, "database": "app"}
    assert "localhost:5432/app" in c._detail()
    red = c.redacted_config()
    assert red["password"] == "***"


async def test_sql_disconnect_closes(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    closed = {"v": False}

    class TrackingConn(_FakeConn):
        def close(self):
            closed["v"] = True

    c = PostgresConnector(config={"host": "localhost"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    monkeypatch.setattr(c, "_open_connection", lambda cfg: TrackingConn())
    await c.connect()
    await c.disconnect()
    assert closed["v"] is True


async def test_sql_unsupported_action(monkeypatch):
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost"})
    result = await c.execute("teleport", {})
    assert not result.ok and "Unsupported" in result.error


# ── MySQL: extra paths ───────────────────────────────────────────────────────


async def test_mysql_list_tables_from_database(monkeypatch):
    from velune.resources.mysql.connector import MySQLConnector

    c = MySQLConnector(config={"host": "127.0.0.1"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    captured: list[str] = []

    class Conn(_FakeConn):
        pass

    def spy_open(cfg):
        return Conn([("t",)], [["users"]])

    monkeypatch.setattr(c, "_open_connection", spy_open)
    orig = c._execute_sql
    monkeypatch.setattr(
        c, "_execute_sql", lambda conn, sql: (captured.append(sql), orig(conn, sql))[1]
    )
    assert (await c.execute("list_tables", {"database": "app"})).ok
    assert any("`app`" in s for s in captured)
    assert not (await c.execute("list_tables", {"database": "bad db"})).ok


# ── Supabase: full lifecycle with mocked transport ───────────────────────────


async def test_supabase_connect_and_actions(monkeypatch):
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    monkeypatch.setattr(c, "driver_available", lambda: True)
    requests: list[tuple[str, str]] = []

    async def fake_request(config, method, path, *, service_role=False, **kw):
        requests.append((method, path))
        if path == "/rest/v1/":
            return ResourceResult.success(data={"definitions": {"users": {"type": "object"}}})
        if path == "/storage/v1/bucket":
            return ResourceResult.success(data=[{"name": "avatars"}])
        return ResourceResult.success(data=[])

    monkeypatch.setattr(c, "_request", fake_request)

    assert (await c.connect()).ok
    assert c.status().state is ResourceState.CONNECTED
    assert (await c.execute("verify", {})).ok
    assert (await c.execute("storage_buckets", {})).ok
    schema = await c.execute("inspect_schema", {"table": "users"})
    assert schema.ok and schema.data["type"] == "object"
    missing = await c.execute("inspect_schema", {"table": "ghost"})
    assert not missing.ok
    q = await c.execute("query", {"table": "users", "limit": 5})
    assert q.ok
    ef = await c.execute("edge_functions", {})
    assert ef.ok and ef.data["functions"] == []
    assert (await c.disconnect()).ok


async def test_supabase_bad_select_rejected(monkeypatch):
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    monkeypatch.setattr(c, "driver_available", lambda: True)

    async def fake_request(config, method, path, **kw):
        return ResourceResult.success(data=[])

    monkeypatch.setattr(c, "_request", fake_request)
    result = await c.execute("query", {"table": "users", "select": "id; DROP"})
    assert not result.ok and "select" in result.error.lower()


async def test_supabase_discover_from_config():
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    hints = await c.discover()
    assert hints and hints[0].detail == "abc.supabase.co"

    empty = SupabaseConnector(config=None)
    assert await empty.discover() == []


async def test_supabase_invalid_url_connect():
    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "http://insecure.example", "anon_key": "k"})
    # driver may or may not be present; force it available to reach URL check.
    c.driver_available = lambda: True  # type: ignore
    result = await c.connect()
    assert not result.ok and "url" in result.error.lower()


# ── Docker: real subprocess handling (never-crash guarantees) ────────────────


class _FakeProc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode = rc
        self._out, self._err = out, err

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass


async def test_docker_run_success_and_error(monkeypatch):
    import velune.resources.docker.connector as mod
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: "docker"))

    async def ok_exec(*args, **kw):
        return _FakeProc(0, out=b"hello\n")

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", ok_exec)
    result = await c._run(["ps"])
    assert result.ok and "hello" in result.data

    async def fail_exec(*args, **kw):
        return _FakeProc(1, err=b"boom")

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fail_exec)
    result = await c._run(["ps"])
    assert not result.ok and "boom" in result.error


async def test_docker_run_oserror(monkeypatch):
    import velune.resources.docker.connector as mod
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: "docker"))

    async def raising(*args, **kw):
        raise OSError("no exec")

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", raising)
    result = await c._run(["ps"])
    assert not result.ok and "Could not run docker" in result.error


async def test_docker_run_timeout(monkeypatch):
    import asyncio

    import velune.resources.docker.connector as mod
    from velune.resources.docker.connector import DockerConnector

    c = DockerConnector()
    monkeypatch.setattr(DockerConnector, "_docker_path", staticmethod(lambda: "docker"))

    class SlowProc(_FakeProc):
        async def communicate(self):
            await asyncio.sleep(5)
            return b"", b""

    async def slow_exec(*args, **kw):
        return SlowProc(0)

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", slow_exec)
    result = await c._run(["ps"], timeout=0.01)
    assert not result.ok and "timed out" in result.error


# ── Supabase: real httpx transport paths ─────────────────────────────────────


def test_supabase_client_header_selection():
    import httpx

    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config=None)
    c._url = "https://abc.supabase.co"
    cfg = {"anon_key": "anon", "service_role_key": "svc"}
    anon_client = c._client(cfg, service_role=False)
    assert anon_client.headers["apikey"] == "anon"
    svc_client = c._client(cfg, service_role=True)
    assert svc_client.headers["apikey"] == "svc"
    assert isinstance(anon_client, httpx.AsyncClient)


async def test_supabase_request_success_and_http_error(monkeypatch):
    import httpx

    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    c._url = "https://abc.supabase.co"

    def ok_handler(request):
        return httpx.Response(200, json={"ok": True})

    def make_ok_client(config, service_role=False):
        return httpx.AsyncClient(
            base_url="https://abc.supabase.co", transport=httpx.MockTransport(ok_handler)
        )

    monkeypatch.setattr(c, "_client", make_ok_client)
    result = await c._request({}, "GET", "/rest/v1/")
    assert result.ok and result.data == {"ok": True}

    def err_handler(request):
        return httpx.Response(404, text="nope")

    def make_err_client(config, service_role=False):
        return httpx.AsyncClient(
            base_url="https://abc.supabase.co", transport=httpx.MockTransport(err_handler)
        )

    monkeypatch.setattr(c, "_client", make_err_client)
    result = await c._request({}, "GET", "/rest/v1/")
    assert not result.ok and "404" in result.error


async def test_supabase_request_network_error(monkeypatch):
    import httpx

    from velune.resources.supabase.connector import SupabaseConnector

    c = SupabaseConnector(config={"url": "https://abc.supabase.co", "anon_key": "k"})
    c._url = "https://abc.supabase.co"

    def boom_handler(request):
        raise httpx.ConnectError("unreachable")

    def make_client(config, service_role=False):
        return httpx.AsyncClient(
            base_url="https://abc.supabase.co", transport=httpx.MockTransport(boom_handler)
        )

    monkeypatch.setattr(c, "_client", make_client)
    result = await c._request({}, "GET", "/rest/v1/")
    assert not result.ok and "unreachable" in result.error


def test_supabase_driver_available_real():
    # httpx is a core dependency, so this exercises the real import branch.
    from velune.resources.supabase.connector import SupabaseConnector

    assert SupabaseConnector().driver_available() is True


# ── DB driver connection building (injected fake drivers) ────────────────────


def test_postgres_open_connection_builds_params(monkeypatch):
    import sys
    import types

    from velune.resources.postgres.connector import PostgresConnector

    captured: dict = {}
    fake = types.ModuleType("psycopg")
    fake.connect = lambda **kw: captured.update(kw) or _FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    c = PostgresConnector()
    assert c.driver_available() is True
    c._open_connection(
        {
            "host": "localhost",
            "port": 5432,
            "database": "app",
            "username": "u",
            "password": "p",
            "sslmode": "require",
        }
    )
    assert captured["dbname"] == "app"
    assert captured["user"] == "u"
    assert captured["sslmode"] == "require"


def test_mysql_open_connection_builds_params(monkeypatch):
    import sys
    import types

    from velune.resources.mysql.connector import MySQLConnector

    captured: dict = {}
    fake = types.ModuleType("pymysql")
    fake.connect = lambda **kw: captured.update(kw) or _FakeConn()
    monkeypatch.setitem(sys.modules, "pymysql", fake)

    c = MySQLConnector()
    assert c.driver_available() is True
    c._open_connection(
        {"host": "127.0.0.1", "port": 3306, "database": "app", "username": "u", "password": "p"}
    )
    assert captured["host"] == "127.0.0.1"
    assert captured["database"] == "app"


def test_db_driver_absent(monkeypatch):
    import builtins

    from velune.resources.mysql.connector import MySQLConnector

    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "pymysql":
            raise ImportError("no pymysql")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert MySQLConnector().driver_available() is False


# ── DB port-probe discovery ──────────────────────────────────────────────────


class _FakeWriter:
    def close(self):
        pass

    async def wait_closed(self):
        pass


async def test_postgres_discover_probe(monkeypatch):
    import asyncio

    from velune.resources.postgres.connector import PostgresConnector

    async def ok_open(host, port):
        return (object(), _FakeWriter())

    monkeypatch.setattr(asyncio, "open_connection", ok_open)
    hints = await PostgresConnector().discover()
    assert hints and hints[0].suggested["port"] == 5432

    async def fail_open(host, port):
        raise OSError("refused")

    monkeypatch.setattr(asyncio, "open_connection", fail_open)
    assert await PostgresConnector().discover() == []


async def test_mysql_discover_probe(monkeypatch):
    import asyncio

    from velune.resources.mysql.connector import MySQLConnector

    async def ok_open(host, port):
        return (object(), _FakeWriter())

    monkeypatch.setattr(asyncio, "open_connection", ok_open)
    hints = await MySQLConnector().discover()
    assert hints and hints[0].resource_id == "mysql"

    async def fail_open(host, port):
        raise OSError("refused")

    monkeypatch.setattr(asyncio, "open_connection", fail_open)
    assert await MySQLConnector().discover() == []


def test_postgres_psycopg2_fallback(monkeypatch):
    import builtins
    import sys
    import types

    from velune.resources.postgres.connector import PostgresConnector

    # psycopg absent, psycopg2 present → both driver_available and
    # _open_connection must fall back to psycopg2.
    real_import = builtins.__import__
    captured: dict = {}
    fake2 = types.ModuleType("psycopg2")
    fake2.connect = lambda **kw: captured.update(kw) or _FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg2", fake2)

    def blocked(name, *a, **k):
        if name == "psycopg":
            raise ImportError("no psycopg")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    c = PostgresConnector()
    assert c.driver_available() is True
    c._open_connection({"host": "localhost", "database": "app", "username": "u", "password": "p"})
    assert captured["dbname"] == "app"


def test_mysql_ssl_branch(monkeypatch):
    import sys
    import types

    from velune.resources.mysql.connector import MySQLConnector

    captured: dict = {}
    fake = types.ModuleType("pymysql")
    fake.connect = lambda **kw: captured.update(kw) or _FakeConn()
    monkeypatch.setitem(sys.modules, "pymysql", fake)
    c = MySQLConnector()
    c._open_connection({"host": "localhost", "ssl_mode": "required"})
    assert "ssl" in captured


def test_manager_config_non_dict_entry():
    # A malformed entry must not crash enabled-resolution.
    m = build_default_manager(config={"docker": "yes"})
    assert "docker" in m.list_ids()  # falls back to enabled


async def test_manager_disconnect_all_swallows_errors():
    class BadDisconnect(FakeConnector):
        async def disconnect(self):
            raise RuntimeError("cannot close")

    m = ResourceManager()
    m.register("fake", BadDisconnect)
    await m.connect("fake")
    await m.disconnect_all()  # must not raise


# ── REPL /resource configure handler ─────────────────────────────────────────


class _FakeResourceManager:
    async def discover(self):
        return []


class _FakeRepl:
    def __init__(self):
        from io import StringIO

        from rich.console import Console

        self.console = Console(file=StringIO())
        self._resource_manager = _FakeResourceManager()


def _canned_prompt(monkeypatch, answers: list[str]):
    """Feed successive canned answers to every Prompt.ask() call in order."""
    it = iter(answers)
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: next(it))


async def test_configure_postgres_saves_encrypted_config(monkeypatch, fake_keystore):
    from velune.cli.handlers.resources import _configure
    from velune.resources.secrets import load_resource_secret

    _canned_prompt(monkeypatch, ["myhost", "5555", "mydb", "myuser", "mypass"])
    await _configure(_FakeRepl(), "postgres")

    cfg = load_resource_secret("postgres", "postgres")
    assert cfg == {
        "host": "myhost",
        "port": 5555,
        "database": "mydb",
        "username": "myuser",
        "password": "mypass",
    }


async def test_configure_supabase_saves_encrypted_config(monkeypatch, fake_keystore):
    from velune.cli.handlers.resources import _configure
    from velune.resources.secrets import load_resource_secret

    _canned_prompt(monkeypatch, ["https://abc.supabase.co", "anon123", ""])
    await _configure(_FakeRepl(), "supabase")

    cfg = load_resource_secret("supabase", "supabase")
    assert cfg == {"url": "https://abc.supabase.co", "anon_key": "anon123"}


async def test_configure_docker_is_a_noop(monkeypatch, fake_keystore):
    from velune.cli.handlers.resources import _configure
    from velune.resources.secrets import load_resource_secret

    def boom(*a, **k):
        raise AssertionError("docker must not prompt for configuration")

    monkeypatch.setattr("rich.prompt.Prompt.ask", boom)
    await _configure(_FakeRepl(), "docker")
    assert load_resource_secret("docker", "docker") is None


async def test_configure_unknown_resource_does_not_save(monkeypatch, fake_keystore):
    from velune.cli.handlers.resources import _configure
    from velune.resources.secrets import load_resource_secret

    def boom(*a, **k):
        raise AssertionError("an unknown resource id must not prompt")

    monkeypatch.setattr("rich.prompt.Prompt.ask", boom)
    await _configure(_FakeRepl(), "redis")
    assert load_resource_secret("redis", "redis") is None


async def test_configure_no_id_does_not_save(fake_keystore):
    from velune.cli.handlers.resources import _configure
    from velune.resources.secrets import load_resource_secret

    await _configure(_FakeRepl(), "")
    assert load_resource_secret("postgres", "postgres") is None
