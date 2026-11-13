"""An in-memory database 'server' for the SQL integration tests (VAIR A5).

``FakeDatabase`` behaves like a DB-API driver connected to a server: it tracks
transactions (pending writes become committed on ``commit`` and are discarded on
``rollback``), enforces ``READ ONLY`` transactions the way PostgreSQL / MySQL do, and
lets a test inject failures, block a statement until it is cancelled, or kill every
connection. It never needs a real driver or a network.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from velune.resources import connections as connections_module
from velune.resources.connections import NamedConnections
from velune.resources.mysql.connector import MySQLConnector
from velune.resources.postgres.connector import PostgresConnector

WRITE_HEADS = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE", "REPLACE")
SECRET_PASSWORD = "s3cr3t-pw-Zq81"


class DriverError(Exception):
    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class OperationalError(DriverError):
    """Named like the real drivers' error: classified as a lost connection."""


class QueryCanceled(DriverError):
    def __init__(self) -> None:
        super().__init__("canceling statement due to user request", "57014")


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.description: list[tuple[str]] | None = None
        self._rows: list[list[Any]] = []
        self._pos = 0
        self.rowcount = -1
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.run(self, sql, params)

    def fetchmany(self, size: int) -> list[list[Any]]:
        rows = self._rows[self._pos : self._pos + size]
        self._pos += len(rows)
        return rows

    def fetchall(self) -> list[list[Any]]:
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, db: FakeDatabase, config: dict[str, Any]) -> None:
        self.db = db
        self.config = config
        self.read_only = False
        self.pending: list[str] = []
        self.dead = False
        self.closed = False
        self.cancel_event = threading.Event()
        self.cursors: list[FakeCursor] = []
        self.in_tx = False

    def cursor(self) -> FakeCursor:
        cur = FakeCursor(self)
        self.cursors.append(cur)
        return cur

    def run(self, cur: FakeCursor, sql: str, params: Any) -> None:
        db = self.db
        if self.closed:
            raise OperationalError("connection already closed")
        if self.dead or db.kill_all:
            self.dead = True
            raise OperationalError("server closed the connection unexpectedly")
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        # Statements Velune issues itself to set a transaction up are not "user SQL".
        if head in ("SET", "START") and sql.upper().startswith(
            ("SET TRANSACTION", "SET LOCAL", "SET SESSION", "START TRANSACTION")
        ):
            db.internal.append(sql)
            if "READ ONLY" in sql.upper():
                self.read_only = True
            elif sql.upper().startswith("START TRANSACTION"):
                self.read_only = False
            self.in_tx = True
            cur.description = None
            return
        db.executed.append((sql, tuple(params) if params is not None else None))
        for needle, exc in list(db.failures.items()):
            if needle in sql:
                raise exc
        for needle, event in list(db.block.items()):
            if needle in sql:
                db.blocked_started.set()
                event.wait(10)
                if self.cancel_event.is_set():
                    raise QueryCanceled()
        if head in WRITE_HEADS:
            if self.read_only:
                raise DriverError("cannot execute a write in a read-only transaction", "25006")
            self.pending.append(sql)
            cur.description = None
            cur.rowcount = 1
            return
        columns, rows = db.answer(sql)
        cur.description = [(c,) for c in columns]
        cur._rows = [list(r) for r in rows]
        cur.rowcount = len(rows)

    def commit(self) -> None:
        if self.dead or self.db.kill_all:
            self.dead = True
            raise OperationalError("connection lost during commit")
        if self.db.commit_error is not None:
            raise self.db.commit_error
        self.db.committed.extend(self.pending)
        self.pending = []
        self.db.commits += 1
        self.read_only = False
        self.in_tx = False

    def rollback(self) -> None:
        self.db.rolled_back.extend(self.pending)
        self.pending = []
        self.db.rollbacks += 1
        self.read_only = False
        self.in_tx = False

    def cancel(self) -> None:
        self.db.cancelled += 1
        self.cancel_event.set()
        for event in self.db.block.values():
            event.set()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.db.closed += 1


class FakeDatabase:
    """The server: shared by every connection a connector opens."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.internal: list[str] = []
        self.committed: list[str] = []
        self.rolled_back: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.opened = 0
        self.closed = 0
        self.cancelled = 0
        self.connections: list[FakeConnection] = []
        self.configs: list[dict[str, Any]] = []
        #: ``substring of SQL -> (columns, rows)`` answers for SELECTs.
        self.catalog: list[tuple[str, tuple[list[str], list[list[Any]]]]] = []
        self.failures: dict[str, Exception] = {}
        self.block: dict[str, threading.Event] = {}
        self.blocked_started = threading.Event()
        self.open_error: Exception | None = None
        self.commit_error: Exception | None = None
        self.kill_all = False

    def on(self, needle: str, columns: list[str], rows: list[list[Any]]) -> None:
        self.catalog.insert(0, (needle, (columns, rows)))

    def answer(self, sql: str) -> tuple[list[str], list[list[Any]]]:
        for needle, result in self.catalog:
            if needle in sql:
                return result
        return ["?column?"], [[1]]

    def connect(self, config: dict[str, Any]) -> FakeConnection:
        if self.open_error is not None:
            raise self.open_error
        conn = FakeConnection(self, dict(config))
        self.connections.append(conn)
        self.configs.append(dict(config))
        self.opened += 1
        return conn

    @property
    def live(self) -> int:
        return sum(1 for c in self.connections if not c.closed)

    def user_sql(self) -> list[str]:
        return [sql for sql, _ in self.executed]

    def revive(self) -> None:
        self.kill_all = False


class FakePostgres(PostgresConnector):
    #: Probe the (fake) server's reachability the way the real connector does (a port probe).
    real_probe = False

    def __init__(self, db: FakeDatabase, **kw: Any) -> None:
        super().__init__(**kw)
        self.db = db
        self._CLIENT_GRACE = 0.2

    def driver_available(self) -> bool:
        return True

    async def probe_availability(self) -> tuple[bool, str]:
        if self.real_probe:
            return await super().probe_availability()
        return True, ""  # no socket in tests: availability is not what they are about

    def _open_connection(self, config: dict[str, Any]) -> Any:
        return self.db.connect(config)


class FakeMySQL(MySQLConnector):
    real_probe = False

    def __init__(self, db: FakeDatabase, **kw: Any) -> None:
        super().__init__(**kw)
        self.db = db
        self._CLIENT_GRACE = 0.2

    def driver_available(self) -> bool:
        return True

    async def probe_availability(self) -> tuple[bool, str]:
        if self.real_probe:
            return await super().probe_availability()
        return True, ""

    def _open_connection(self, config: dict[str, Any]) -> Any:
        return self.db.connect(config)

    def _cancel(self, conn: Any) -> None:
        conn.cancel()


class SQLEnv:
    """A fake keystore plus a fake server, wired into the connectors."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.db = FakeDatabase()
        self.store: dict[tuple[str, str], dict[str, Any]] = {}
        monkeypatch.setattr(
            connections_module, "load_resource_secret", lambda t, n: self.store.get((t, n))
        )
        monkeypatch.setattr(
            NamedConnections,
            "_stored_names",
            lambda inst: sorted(n for (t, n) in self.store if t == inst.resource_id),
        )

    def add(self, name: str = "postgres", engine: str = "postgres", **cfg: Any) -> dict[str, Any]:
        record = {
            "host": "localhost",
            "port": 5432 if engine == "postgres" else 3306,
            "database": "app",
            "username": "dev",
            "password": SECRET_PASSWORD,
            **cfg,
        }
        self.store[(engine, name)] = record
        return record

    def settings(self, **kw: Any) -> dict[str, Any]:
        return {"statement_timeout": 5.0, **kw}

    def pg(self, settings: dict[str, Any] | None = None, **kw: Any) -> FakePostgres:
        return FakePostgres(
            self.db,
            settings=settings if settings is not None else self.settings(),
            workspace=self.tmp_path,
            workspace_trusted=kw.pop("workspace_trusted", True),
            **kw,
        )

    def mysql(self, settings: dict[str, Any] | None = None, **kw: Any) -> FakeMySQL:
        return FakeMySQL(
            self.db,
            settings=settings if settings is not None else self.settings(),
            workspace=self.tmp_path,
            workspace_trusted=kw.pop("workspace_trusted", True),
            **kw,
        )
