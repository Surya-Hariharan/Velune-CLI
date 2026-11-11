"""Shared base for the SQL database integrations (PostgreSQL, MySQL/MariaDB).

Everything the two engines have in common lives here, so each differs only in its
driver and its catalog SQL:

* **named connections** — one connector serves any number of configured connections
  (``connection="staging"``). A connection's endpoint and password live together in
  the encrypted keystore; configuration only names a credential and sets policy
  (``read_only``, ``environment``, limits), so a repository cannot redirect a stored
  password;
* **local by default** — a remote host is refused unless the *stored* credential opts
  in and requires TLS, because these connectors hold passwords;
* **one statement per call**, tokenized and classified structurally
  (:mod:`velune.resources.sql_parse`); anything administrative is blocked, never offered;
* **bound parameters** — values travel as driver parameters, never spliced into text;
* **engine-enforced read-only** for read operations (a ``READ ONLY`` transaction, always
  rolled back), server-side statement timeouts, client-side deadlines with query
  cancellation, and bounded, redacted, JSON-safe results;
* **explicit transactions** (begin / execute / commit / rollback) that cannot be
  committed after a failure, are rolled back when idle or when the connection is lost,
  and are never left open at disconnect.

Drivers (``psycopg`` / ``pymysql``) are imported lazily by the subclasses, so importing
this module pulls no database dependency and a missing driver degrades to a structured
"driver not installed" result.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
import uuid
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from velune.resources.base import (
    ErrorCode,
    Lifecycle,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.connections import (
    NamedConnections,
    Profile,
    _coerce_settings,
    _workspace_trusted,
)
from velune.resources.secrets import redact_config
from velune.resources.sql_parse import (
    Category,
    Dialect,
    SQLAnalysis,
    StatementError,
    analyze_sql,
    rank,
    require_single_statement,
)
from velune.resources.sql_results import ResultLimits, shape_rows
from velune.resources.sql_safety import verdict_for

logger = logging.getLogger("velune.resources.sql")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]{1,63}$")
_PASSWORD_IN_TEXT = re.compile(r"(password|pwd)\s*=\s*\S+", re.IGNORECASE)
_SECURE_SSL = frozenset(
    {"require", "required", "verify-ca", "verify_ca", "verify-full", "verify_identity"}
)
_MAX_PARAMS = 64
_MAX_PARAM_CHARS = 10_000
_MAX_PAGE = 200

# Permission scopes (see velune.permissions.actions).
SCOPE_SCHEMA_READ = "database.schema.read"
SCOPE_QUERY_READ = "database.query.read"
SCOPE_QUERY_WRITE = "database.query.write"
SCOPE_SCHEMA_WRITE = "database.schema.write"
SCOPE_QUERY_DESTRUCTIVE = "database.query.destructive"
SCOPE_TRANSACTION = "database.transaction"
SCOPE_CONNECTION = "database.connection.read"
SCOPE_HEALTH = "database.health"
SCOPE_ADMIN_BLOCKED = "database.admin"

#: Operations that run caller-written SQL (and so are classified from the statement).
_SQL_ACTIONS = frozenset({"query", "execute", "explain", "transaction_execute"})


# ── Errors ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DbError:
    code: ErrorCode
    message: str
    hint: str = ""
    #: The connection itself is gone (not just this statement).
    connection_lost: bool = False


_PG_CLASSES: dict[str, tuple[ErrorCode, str]] = {
    "28": (ErrorCode.AUTHENTICATION_FAILED, "Check the stored username and password."),
    "42501": (ErrorCode.AUTHORIZATION_DENIED, "The database role lacks the privilege."),
    "42P01": (ErrorCode.NOT_FOUND, "Check the table name and schema (list_tables)."),
    "42703": (ErrorCode.VALIDATION_FAILED, "Check the column names (describe_table)."),
    "3D": (ErrorCode.NOT_FOUND, "That database does not exist."),
    "3F": (ErrorCode.NOT_FOUND, "That schema does not exist."),
    "57014": (ErrorCode.TIMEOUT, "Narrow the query or raise the statement timeout."),
    "25006": (ErrorCode.AUTHORIZATION_DENIED, "The transaction or connection is read-only."),
    "23": (ErrorCode.CONFLICT, "A constraint rejected the change."),
    "40": (ErrorCode.CONFLICT, "Deadlock or serialization failure: retry the transaction."),
    "42": (ErrorCode.VALIDATION_FAILED, "The SQL is invalid for this database."),
    "08": (ErrorCode.CONNECTION_FAILED, "The connection to the database was lost."),
    "53300": (ErrorCode.UNAVAILABLE, "The server has too many connections."),
    "57P01": (ErrorCode.CONNECTION_FAILED, "The server shut down the connection."),
}
_MYSQL_ERRNO: dict[int, tuple[ErrorCode, str]] = {
    1045: (ErrorCode.AUTHENTICATION_FAILED, "Check the stored username and password."),
    1044: (ErrorCode.AUTHORIZATION_DENIED, "The database user lacks access to that database."),
    1142: (ErrorCode.AUTHORIZATION_DENIED, "The database user lacks that privilege."),
    1143: (ErrorCode.AUTHORIZATION_DENIED, "The database user lacks that privilege."),
    1049: (ErrorCode.NOT_FOUND, "That database does not exist."),
    1146: (ErrorCode.NOT_FOUND, "Check the table name (list_tables)."),
    1054: (ErrorCode.VALIDATION_FAILED, "Check the column names (describe_table)."),
    1064: (ErrorCode.VALIDATION_FAILED, "The SQL is invalid for this database."),
    1062: (ErrorCode.CONFLICT, "A unique constraint rejected the change."),
    1451: (ErrorCode.CONFLICT, "A foreign key constraint rejected the change."),
    1452: (ErrorCode.CONFLICT, "A foreign key constraint rejected the change."),
    1213: (ErrorCode.CONFLICT, "Deadlock: retry the transaction."),
    1205: (ErrorCode.CONFLICT, "Lock wait timeout: retry later."),
    1792: (ErrorCode.AUTHORIZATION_DENIED, "The transaction is read-only."),
    3024: (ErrorCode.TIMEOUT, "Narrow the query or raise the statement timeout."),
    1317: (ErrorCode.TIMEOUT, "The query was interrupted."),
    2002: (ErrorCode.CONNECTION_FAILED, "Is the database server running?"),
    2003: (ErrorCode.CONNECTION_FAILED, "Is the database server running?"),
    2006: (ErrorCode.CONNECTION_FAILED, "The connection to the database was lost."),
    2013: (ErrorCode.CONNECTION_FAILED, "The connection to the database was lost."),
}


_CONNECTION_ERROR_NAMES = frozenset({"InterfaceError", "OperationalError"})


def sanitize_text(text: str, limit: int = 300) -> str:
    """Strip ``password=…`` fragments and secret-shaped text; bound the length."""
    from velune.core.redaction import redact_secrets

    return redact_secrets(_PASSWORD_IN_TEXT.sub(r"\1=***", str(text))).strip()[:limit]


def classify_db_error(exc: BaseException) -> DbError:
    """Normalize any driver exception. Never includes SQL values or credentials."""
    name = type(exc).__name__
    state = str(getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None) or "")
    errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    # Only the primary message: DETAIL/CONTEXT lines can echo row values.
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    message = str(primary or (str(exc).splitlines() or [""])[0])
    if errno is not None and len(exc.args) > 1:
        message = str(exc.args[1])
    code: ErrorCode | None = None
    hint = ""
    # A MySQL driver error carries an integer error number, which is more specific than the
    # SQLSTATE (PyMySQL >= 1.2 also sets ``sqlstate``, often the generic ``HY000``), so it
    # is consulted first. PostgreSQL errors have no integer ``args[0]``.
    if errno in _MYSQL_ERRNO:
        code, hint = _MYSQL_ERRNO[errno]  # type: ignore[index]
    elif state:
        for key in (state, state[:2]):
            if key in _PG_CLASSES:
                code, hint = _PG_CLASSES[key]
                break
    if code is None:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            code, hint = ErrorCode.TIMEOUT, "Narrow the query or raise the statement timeout."
        elif isinstance(exc, (ConnectionError, OSError)) or _CONNECTION_ERROR_NAMES & {
            cls.__name__ for cls in type(exc).__mro__
        }:
            # Judged by the whole class hierarchy: psycopg 3's ConnectionTimeout, for
            # example, is an OperationalError.
            code, hint = ErrorCode.CONNECTION_FAILED, "Is the database server running?"
        else:
            code = ErrorCode.ADAPTER_FAILURE
    lost = code is ErrorCode.CONNECTION_FAILED
    return DbError(code, sanitize_text(f"{name}: {message}"), hint, lost)


# ── Connection model ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Tx:
    id: str
    read_only: bool
    started: float
    statements: int = 0
    failed: bool = False
    timer: asyncio.TimerHandle | None = None


class _Session:
    """Live state of one named connection (the DB-API connection is not thread safe)."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.conn: Any = None
        self.state = ResourceState.DISCONNECTED
        self.error: str | None = None
        self.info: dict[str, Any] = {}
        self.lock = asyncio.Lock()
        self.tx: _Tx | None = None


@dataclass(slots=True)
class RawResult:
    columns: list[str]
    rows: list[list[Any]]
    rowcount: int = -1


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


class SQLConnector(NamedConnections, ResourceConnector):
    """Common lifecycle + gated SQL execution for relational databases."""

    #: One live driver connection per named connection, reused across calls.
    lifecycle = Lifecycle.PERSISTENT
    default_port: int = 5432
    driver_name: str = ""
    dialect: Dialect = Dialect.GENERIC
    #: Install hint shown when the driver is missing.
    install_hint: str = ""

    def __init__(
        self,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        *,
        settings: Any = None,
        workspace: Any = None,
        workspace_trusted: bool | None = None,
    ) -> None:
        super().__init__(name)
        # An explicit config is the legacy single, unnamed connection.
        self._explicit_config = config
        self.settings = _coerce_settings(settings)
        self.workspace = workspace
        self._trusted = _workspace_trusted(workspace, workspace_trusted)
        self._sessions: dict[str, _Session] = {}
        self._create_lock = asyncio.Lock()
        self._tx_by_id: dict[str, str] = {}
        self._info: dict[str, Any] = {}

    # ── Driver hooks (implemented per engine) ────────────────────────────────

    @abstractmethod
    def driver_available(self) -> bool:
        """True when the engine's Python driver can be imported."""

    @abstractmethod
    def _open_connection(self, config: dict[str, Any]) -> Any:
        """Open a live driver connection (runs in a worker thread). May raise."""

    @abstractmethod
    def _engine_capabilities(self) -> list[ResourceCapability]:
        """Engine-specific catalog capabilities."""

    @abstractmethod
    def _schema_sql(self, op: str, args: dict[str, Any]) -> tuple[str, list[Any]]:
        """``(sql, params)`` for a catalog operation (all values bound, never spliced)."""

    def _begin(self, conn: Any, *, read_only: bool, timeout_ms: int, rows: int) -> None:
        """Start a transaction in the requested mode with a server-side timeout."""

    def _cancel(self, conn: Any) -> None:
        """Ask the server to stop the running statement (called from another thread)."""

    def _open_cursor(self, conn: Any, *, stream: bool) -> Any:
        return conn.cursor()

    # ── Locality / TLS policy

    def _is_remote(self, stored: dict[str, Any]) -> bool:
        return not self.is_local_host(stored.get("host", "localhost"))

    @staticmethod
    def is_local_host(host: str | None) -> bool:
        """True only for loopback hosts or a unix-socket path."""
        if host is None:
            return True
        if not isinstance(host, str):
            return False  # malformed: never "local"; validation reports it
        h = host.strip()
        if not h:
            return True
        if h.startswith("/"):
            return True
        h = h.lower().strip("[]")
        if h in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            return ipaddress.ip_address(h).is_loopback
        except ValueError:
            return False

    _SSL_MODES = frozenset(
        {
            "disable", "disabled", "false", "allow", "prefer", "preferred", "require",
            "required", "verify-ca", "verify_ca", "verify-full", "verify_identity",
        }
    )  # fmt: skip

    @classmethod
    def _config_problem(cls, cfg: dict[str, Any]) -> str | None:
        """Why a stored connection is malformed, or None. Never echoes a value."""
        host = cfg.get("host", "localhost")
        if host not in (None, ""):
            if (
                not isinstance(host, str)
                or len(host) > 255
                or re.search(r"[\s\x00-\x1f;'\"`\\]", host)
            ):
                return "The stored host is not a valid host name, address or socket path."
        port = cfg.get("port")
        if port not in (None, ""):
            try:
                number = int(port)
            except (TypeError, ValueError):
                return "The stored port must be a number."
            if isinstance(port, bool) or not 1 <= number <= 65535:
                return "The stored port must be between 1 and 65535."
        for key in ("database", "username"):
            value = cfg.get(key)
            if value not in (None, "") and (
                not isinstance(value, str) or len(value) > 128 or "\x00" in value
            ):
                return f"The stored {key} is not valid."
        mode = cfg.get("sslmode") or cfg.get("ssl_mode")
        if mode and str(mode).lower() not in cls._SSL_MODES:
            return "The stored TLS mode is not recognised (use require, verify-ca or verify-full)."
        return None

    def _policy_problem(self, profile: Profile) -> str | None:
        cfg = profile.config
        host = cfg.get("host", "localhost")
        if self.is_local_host(host):
            return None
        if not cfg.get("allow_remote"):
            return (
                f"Refusing to connect to non-local host '{host}'. The {self.display_name} "
                "connector supports local instances unless the stored connection allows "
                "a remote host with TLS."
            )
        mode = str(cfg.get("sslmode") or cfg.get("ssl_mode") or "").lower()
        if mode not in _SECURE_SSL:
            return (
                f"Refusing to send credentials to remote host '{host}' without TLS: "
                "the connection needs sslmode=require (or verify-full)."
            )
        return None

    @staticmethod
    def _sanitize(text: str) -> str:
        """Strip any ``password=...`` fragment a driver may echo in an error."""
        return _PASSWORD_IN_TEXT.sub(r"\1=***", text)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _session(self, profile: Profile) -> _Session:
        sess = self._sessions.get(profile.name)
        if sess is None:
            sess = self._sessions[profile.name] = _Session(profile)
        else:
            sess.profile = profile
        return sess

    async def connect(self) -> ResourceResult:
        """Connect the default connection (or every configured one when ambiguous)."""
        names = self.connection_names()
        default = self._default_name(names)
        if default is not None or len(names) <= 1:
            return await self._connect_named(default)
        results = [await self._connect_named(n) for n in names]
        bad = [n for n, r in zip(names, results, strict=True) if not r.ok]
        if bad:
            return ResourceResult.failure(
                f"Could not connect: {', '.join(bad)}.",
                action="connect",
                code=ErrorCode.CONNECTION_FAILED,
            )
        return ResourceResult.success("connect", {"connections": names})

    async def _connect_named(self, name: str | None) -> ResourceResult:
        if not self.driver_available():
            self._state_hint = ResourceState.UNAVAILABLE
            return ResourceResult.failure(
                f"{self.driver_name} driver is not installed. "
                f"Install it to use the {self.display_name} connector."
                + (f" ({self.install_hint})" if self.install_hint else ""),
                action="connect",
                code=ErrorCode.UNAVAILABLE,
            )
        profile, failure = self._profile_for(name)
        if profile is None:
            assert failure is not None
            failure.action = "connect"
            if not self._sessions:
                self._state_hint = ResourceState.DISCONNECTED
            return failure
        sess = self._session(profile)
        async with sess.lock:
            return await self._connect_locked(sess)

    _state_hint: ResourceState | None = None
    #: Extra seconds the client waits beyond the server-side statement timeout.
    _CLIENT_GRACE = 2.0

    async def _connect_locked(self, sess: _Session) -> ResourceResult:
        profile = sess.profile
        invalid = self._config_problem(profile.config)
        if invalid:
            sess.state, sess.error = ResourceState.ERROR, invalid
            return ResourceResult.failure(
                invalid,
                action="connect",
                code=ErrorCode.VALIDATION_FAILED,
                hint=f"Store it again with /resource configure {self.resource_id} {profile.name}.",
            )
        problem = self._policy_problem(profile)
        if problem:
            sess.state, sess.error = ResourceState.ERROR, problem
            return ResourceResult.failure(
                problem, action="connect", code=ErrorCode.AUTHORIZATION_DENIED
            )
        if sess.conn is not None and sess.state is ResourceState.CONNECTED:
            return ResourceResult.success("connect", dict(sess.info))
        sess.state = ResourceState.CONNECTING
        try:
            sess.conn = await asyncio.wait_for(
                asyncio.to_thread(self._open_connection, profile.config),
                timeout=profile.connect_timeout + 5.0,
            )
        except asyncio.TimeoutError:
            sess.conn = None
            sess.state = ResourceState.ERROR
            sess.error = "Timed out opening the database connection."
            return ResourceResult.failure(
                sess.error,
                action="connect",
                code=ErrorCode.TIMEOUT,
                hint="Is the database server running and reachable?",
            )
        except Exception as exc:
            sess.conn = None
            err = classify_db_error(exc)
            sess.state, sess.error = ResourceState.ERROR, err.message
            return ResourceResult.failure(
                err.message, action="connect", code=err.code, hint=err.hint
            )
        sess.state, sess.error = ResourceState.CONNECTED, None
        cfg = profile.config
        sess.info = {
            "host": cfg.get("host", "localhost"),
            "port": cfg.get("port", self.default_port),
            "database": cfg.get("database"),
        }
        self._info = dict(sess.info)
        self._state_hint = None
        return ResourceResult.success("connect", dict(sess.info))

    async def disconnect(self) -> ResourceResult:
        """Roll back anything open and close every connection. Never leaves one open."""
        for sess in list(self._sessions.values()):
            async with sess.lock:
                await self._drop(sess, rollback=True)
        self._sessions.clear()
        self._tx_by_id.clear()
        self._state_hint = None
        return ResourceResult.success("disconnect")

    async def _drop(self, sess: _Session, *, rollback: bool, abort: bool = False) -> None:
        """Release one session's connection (and its transaction) reliably."""
        self._cancel_timer(sess)
        conn, sess.conn = sess.conn, None
        if sess.tx is not None:
            self._tx_by_id.pop(sess.tx.id, None)
            sess.tx = None
        if conn is not None:
            if abort:
                await asyncio.to_thread(self._quiet, self._cancel, conn)
            elif rollback:
                await asyncio.to_thread(self._quiet, self._rollback, conn)
            await asyncio.to_thread(self._quiet, conn.close)
        sess.state = ResourceState.DISCONNECTED

    @staticmethod
    def _quiet(fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:
            logger.debug("ignored error during cleanup: %s", type(exc).__name__)

    @staticmethod
    def _try_execute(cur: Any, statement: str) -> bool:
        """Run an optional setup statement; False (not an error) if the server rejects it."""
        try:
            cur.execute(statement)
        except Exception as exc:
            logger.debug("optional statement not supported: %s", type(exc).__name__)
            return False
        return True

    @staticmethod
    def _rollback(conn: Any) -> None:
        conn.rollback()

    def status(self) -> ResourceStatus:
        connected = [s for s in self._sessions.values() if s.state is ResourceState.CONNECTED]
        errored = [s for s in self._sessions.values() if s.state is ResourceState.ERROR]
        if connected:
            state = ResourceState.CONNECTED
        elif errored:
            state = ResourceState.ERROR
        elif self._state_hint is not None:
            state = self._state_hint
        else:
            state = ResourceState.DISCONNECTED
        error = errored[0].error if errored and not connected else None
        return ResourceStatus(
            resource_id=self.resource_id,
            display_name=self.display_name,
            state=state,
            detail=self._detail(),
            info=dict(self._info),
            error=error,
        )

    def _detail(self) -> str:
        if self._info.get("host"):
            db = self._info.get("database") or ""
            return f"{self._info['host']}:{self._info.get('port', self.default_port)}/{db}".rstrip(
                "/"
            )
        return ""

    async def health(self) -> bool:
        return any(s.state is ResourceState.CONNECTED for s in self._sessions.values())

    # ── Capabilities ─────────────────────────────────────────────────────────

    _CONN_PARAM = {
        "type": "string",
        "description": "Named connection (see list_connections). Omit to use the default.",
    }
    _SQL_PARAMS = {
        "sql": {
            "type": "string",
            "description": "ONE SQL statement. Put values in params and use %s placeholders.",
        },
        "params": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Positional values bound to the %s placeholders (never inline them).",
        },
        "timeout_seconds": {
            "type": "number",
            "description": "Lower statement timeout for this call.",
            "minimum": 1,
            "maximum": 600,
        },
        "connection": _CONN_PARAM,
    }

    def _sql_params(
        self, *, with_rows: bool, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        props = {k: dict(v) for k, v in self._SQL_PARAMS.items()}
        if with_rows:
            props["max_rows"] = {
                "type": "integer",
                "description": "Most rows to return.",
                "minimum": 1,
                "maximum": 5000,
            }
        props.update(extra or {})
        return {"properties": props, "required": ["sql"]}

    def capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        write = ResourcePermission.WRITE
        conn = {"properties": {"connection": dict(self._CONN_PARAM)}}
        tx_param = {
            "type": "string",
            "description": "Id returned by transaction_begin.",
        }
        return [
            ResourceCapability(
                "list_connections",
                read,
                "List the configured database connections (no credentials)",
                scope=SCOPE_CONNECTION,
                category="connections",
            ),
            ResourceCapability(
                "health",
                read,
                "Check a connection is alive (and its latency)",
                scope=SCOPE_HEALTH,
                category="connections",
                params=conn,
            ),
            ResourceCapability(
                "diagnose",
                read,
                "Explain why a connection fails: driver, config, TLS policy, reachability, login",
                scope=SCOPE_CONNECTION,
                category="connections",
                params=conn,
            ),
            *self._engine_capabilities(),
            ResourceCapability(
                "query",
                read,
                "Run ONE read-only SQL statement (SELECT/EXPLAIN/SHOW). Values go in params",
                scope=SCOPE_QUERY_READ,
                category="query_read",
                params=self._sql_params(with_rows=True),
            ),
            ResourceCapability(
                "explain",
                read,
                "Show the query plan for a statement without running it",
                scope=SCOPE_QUERY_READ,
                category="query_read",
                params=self._sql_params(with_rows=False),
            ),
            ResourceCapability(
                "execute",
                write,
                "Run ONE statement that changes data or schema (INSERT/UPDATE/DELETE/DDL). Needs approval",
                scope=SCOPE_QUERY_WRITE,
                category="query_write",
                params=self._sql_params(with_rows=True),
            ),
            ResourceCapability(
                "transaction_begin",
                read,
                "Start a transaction so several statements commit or roll back together",
                scope=SCOPE_TRANSACTION,
                category="transactions",
                params={
                    "properties": {
                        "read_only": {
                            "type": "boolean",
                            "description": "Open a read-only transaction (no changes possible).",
                        },
                        "connection": dict(self._CONN_PARAM),
                    }
                },
            ),
            ResourceCapability(
                "transaction_execute",
                read,
                "Run ONE statement inside an open transaction (each is approved separately)",
                scope=SCOPE_TRANSACTION,
                category="transactions",
                params={
                    "properties": {
                        "transaction_id": tx_param,
                        **self._sql_params(with_rows=True)["properties"],
                    },
                    "required": ["transaction_id", "sql"],
                },
            ),
            ResourceCapability(
                "transaction_commit",
                read,
                "Commit an open transaction (refused after a failed statement)",
                scope=SCOPE_TRANSACTION,
                category="transactions",
                params={"properties": {"transaction_id": tx_param}, "required": ["transaction_id"]},
            ),
            ResourceCapability(
                "transaction_rollback",
                read,
                "Roll back an open transaction, discarding its changes",
                scope=SCOPE_TRANSACTION,
                category="transactions",
                params={"properties": {"transaction_id": tx_param}, "required": ["transaction_id"]},
            ),
        ]

    def configuration_problem(self) -> str | None:
        if self.connection_names() or (
            self.settings.connections and any(c.host for c in self.settings.connections.values())
        ):
            return None
        return f"Not configured — the user must run /resource configure {self.resource_id}."

    async def probe_availability(self) -> tuple[bool, str]:
        if not self.driver_available():
            return False, f"the {self.driver_name} driver is not installed"
        profile, failure = self._profile_for(None)
        if profile is None:
            # Several connections and no default still means "usable": name one.
            return (True, "") if self.connection_names() else (False, "not configured")
        host = profile.config.get("host") or "localhost"
        if str(host).startswith("/"):
            return True, ""
        try:
            port = int(profile.config.get("port") or self.default_port)
            fut = asyncio.open_connection(str(host), port)
            _reader, writer = await asyncio.wait_for(fut, timeout=1.5)
            writer.close()
        except (OSError, asyncio.TimeoutError, ValueError):
            return False, "the database server is not reachable"
        return True, ""

    # ── Authorization description ────────────────────────────────────────────

    def _dialect_analysis(self, sql: str) -> SQLAnalysis:
        return analyze_sql(sql, self.dialect)

    def _sql_text(self, action: str, params: dict[str, Any] | None) -> str:
        p = params or {}
        sql = str(p.get("sql") or p.get("query") or "")
        return f"EXPLAIN {sql}" if action == "explain" else sql

    def _scope_for_category(self, category: Category) -> str:
        return {
            Category.READ: SCOPE_QUERY_READ,
            Category.WRITE: SCOPE_QUERY_WRITE,
            Category.SCHEMA: SCOPE_SCHEMA_WRITE,
            Category.DESTRUCTIVE: SCOPE_QUERY_DESTRUCTIVE,
            Category.BLOCKED: SCOPE_ADMIN_BLOCKED,
        }[category]

    def scope_for(self, action, params, permission):
        if action in _SQL_ACTIONS and params is not None:
            category = self._dialect_analysis(self._sql_text(action, params)).category
            if action == "transaction_execute":
                return (
                    SCOPE_TRANSACTION
                    if category is Category.READ
                    else self._scope_for_category(category)
                )
            return self._scope_for_category(category)
        if action in ("query", "explain"):
            return SCOPE_QUERY_READ
        if action == "execute":
            return SCOPE_QUERY_WRITE
        cap = self.capability_for(action)
        if cap is not None and cap.scope:
            return cap.scope
        return SCOPE_SCHEMA_READ

    def effective_permission(
        self, action: str, params: dict[str, Any] | None
    ) -> tuple[ResourcePermission, bool] | None:
        """Raise the declared tier to what the statement actually needs."""
        if action in _SQL_ACTIONS:
            verdict = verdict_for(self._dialect_analysis(self._sql_text(action, params)))
            return (verdict.permission, verdict.permission is ResourcePermission.ADMIN)
        p = params or {}
        if action == "transaction_begin" and not p.get("read_only"):
            return (ResourcePermission.WRITE, False)
        if action == "transaction_commit":
            tx = self._tx_lookup(p)
            if tx is None or not tx.read_only:  # unknown: assume it changed something
                return (ResourcePermission.WRITE, False)
        return None

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        p = params or {}
        target = self._preview_target(p)
        if action in _SQL_ACTIONS:
            sql = str(p.get("sql") or p.get("query") or "")[:300]
            bound = len(p.get("params") or []) if isinstance(p.get("params"), list) else 0
            suffix = f"  [{bound} bound value{'s' if bound != 1 else ''}]" if bound else ""
            return f"{target} | {sql}{suffix}"
        if action.startswith("transaction"):
            return f"{target} | {action}"
        return target if p.get("connection") else ""

    def _preview_target(self, p: dict[str, Any]) -> str:
        """``connection=name db=app env=production`` — what the user is approving against."""
        try:
            profile, _ = self._profile_for(p.get("connection"))
        except Exception:
            profile = None
        if profile is None:
            return f"connection={str(p.get('connection') or '?')[:40]}"
        bits = [f"connection={profile.name}", f"db={profile.config.get('database') or '-'}"]
        host = profile.config.get("host") or "localhost"
        bits.append(f"host={host}")
        if profile.environment:
            bits.append(f"env={profile.environment}")
        return " ".join(bits)

    def describe_actions(self, action, params, *, ledger=None):
        """Permission-gate actions for one call: precise scope, honest risk.

        Every operation that changes anything is HIGH risk, so AUTO mode asks; PLAN
        denies it; reads on any connection need no confirmation. Statements that are
        administrative, or that target a read-only connection, are *blocked* in every
        mode.
        """
        from velune.permissions.actions import Action as GateAction
        from velune.permissions.actions import ActionType, Risk
        from velune.resources.base import _safe_preview

        cap = self.capability_for(action)
        if cap is None:
            return []
        p = params or {}
        detail = _safe_preview(self, action, p)
        target = f"{self.name}:{action}"
        profile, _failure = (
            self._profile_for(p.get("connection")) if action != "list_connections" else (None, None)
        )
        read_only_conn = bool(profile and profile.read_only)
        reasons: list[str] = []
        if profile is not None:
            if profile.production:
                reasons.append("production connection")
            if profile.remote:
                reasons.append("remote server")

        if action in _SQL_ACTIONS:
            analysis = self._dialect_analysis(self._sql_text(action, p))
            category = analysis.category
            scope = self.scope_for(action, p, ResourcePermission.READ)
            blocked = ""
            if category is Category.BLOCKED:
                blocked = f"administrative statement is never run here: {analysis.reason}"
            elif not analysis.well_formed or analysis.count != 1:
                blocked = (
                    analysis.reason
                    if not analysis.well_formed
                    else (
                        "exactly one statement per call is allowed"
                        if analysis.count
                        else "empty statement"
                    )
                )
            elif action in ("query", "explain") and category is not Category.READ:
                blocked = "query is read-only; use execute for statements that change data"
            elif action != "query" and read_only_conn and category is not Category.READ:
                blocked = f"connection '{profile.name if profile else ''}' is read-only"
            if analysis.unbounded and category is not Category.READ:
                reasons.append("affects every row (no WHERE)")
            if category is Category.READ and not blocked:
                gate = (ActionType.INTEGRATION_READ, Risk.LOW)
            elif category in (Category.DESTRUCTIVE, Category.BLOCKED):
                gate = (ActionType.INTEGRATION_DELETE, Risk.HIGH)
            else:
                gate = (ActionType.INTEGRATION_WRITE, Risk.HIGH)
            why = "; ".join([analysis.reason, *reasons]) if reasons else analysis.reason
            return [
                GateAction(
                    gate[0],
                    target,
                    why[:200],
                    gate[1],
                    blocked=bool(blocked),
                    detail=(f"{detail}  ({blocked})" if blocked else detail),
                    scope=scope,
                )
            ]

        if action in ("transaction_begin", "transaction_commit"):
            tx = self._tx_lookup(p)
            read = (
                bool(p.get("read_only"))
                if action == "transaction_begin"
                else bool(tx and tx.read_only)
            )
            if read:
                return [
                    GateAction(
                        ActionType.INTEGRATION_READ,
                        target,
                        cap.description,
                        Risk.LOW,
                        detail=detail,
                        scope=SCOPE_TRANSACTION,
                    )
                ]
            blocked_tx = read_only_conn and action == "transaction_begin"
            return [
                GateAction(
                    ActionType.INTEGRATION_WRITE,
                    target,
                    "; ".join([cap.description, *reasons])[:200],
                    Risk.HIGH,
                    blocked=bool(blocked_tx),
                    detail=detail,
                    scope=SCOPE_TRANSACTION,
                )
            ]
        # Reads: catalog, health, diagnose, listing, rollback.
        scope = self.scope_for(action, p, ResourcePermission.READ)
        return [
            GateAction(
                ActionType.INTEGRATION_READ,
                target,
                cap.description,
                Risk.LOW,
                detail=detail,
                scope=scope,
            )
        ]

    def _tx_lookup(self, p: dict[str, Any]) -> _Tx | None:
        tid = p.get("transaction_id")
        if not isinstance(tid, str):
            return None
        name = self._tx_by_id.get(tid)
        sess = self._sessions.get(name or "")
        return sess.tx if sess and sess.tx and sess.tx.id == tid else None

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = params or {}
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(
                f"Unsupported action '{action}'.",
                action=action,
                code=ErrorCode.UNSUPPORTED_OPERATION,
            )
        try:
            result = await handler(params)
        except Exception as exc:  # a connector never raises across this boundary
            logger.debug("sql action %s raised", action, exc_info=True)
            err = classify_db_error(exc)
            result = ResourceResult.failure(
                err.message, action=action, code=err.code, hint=err.hint
            )
        if not result.action:
            result.action = action
        return result

    # -- connection plumbing ------------------------------------------------

    async def _ready(self, name: Any) -> tuple[_Session | None, ResourceResult | None]:
        """A connected session for *name* (connecting lazily), or a failure result."""
        if name is not None and not isinstance(name, str):
            return None, ResourceResult.failure(
                "'connection' must be a string.", code=ErrorCode.VALIDATION_FAILED
            )
        if not self.driver_available():
            return None, ResourceResult.failure(
                f"{self.driver_name} driver is not installed. Install it to use the {self.display_name} connector.",
                code=ErrorCode.UNAVAILABLE,
            )
        profile, failure = self._profile_for(name)
        if profile is None:
            return None, failure
        sess = self._session(profile)
        if sess.conn is None or sess.state is not ResourceState.CONNECTED:
            async with sess.lock:
                result = await self._connect_locked(sess)
            if not result.ok:
                return None, result
        return sess, None

    def _cancel_timer(self, sess: _Session) -> None:
        if sess.tx is not None and sess.tx.timer is not None:
            sess.tx.timer.cancel()
            sess.tx.timer = None

    def _arm_timer(self, sess: _Session) -> None:
        """(Re)start the idle clock of an open transaction."""
        tx = sess.tx
        if tx is None:
            return
        self._cancel_timer(sess)
        loop = asyncio.get_running_loop()
        tx.timer = loop.call_later(
            sess.profile.idle_transaction_timeout,
            lambda: asyncio.ensure_future(self._expire(sess, tx.id)),
        )

    async def _expire(self, sess: _Session, tx_id: str) -> None:
        """Idle-timeout: roll back so a forgotten transaction never holds locks."""
        async with sess.lock:
            if sess.tx is not None and sess.tx.id == tx_id:
                logger.info("rolling back idle transaction on connection %s", sess.profile.name)
                await self._drop_tx(sess, reason="idle")

    async def _drop_tx(self, sess: _Session, *, reason: str) -> None:
        conn = sess.conn
        if sess.tx is not None:
            self._cancel_timer(sess)
            self._tx_by_id.pop(sess.tx.id, None)
            sess.tx = None
        if conn is not None:
            await asyncio.to_thread(self._quiet, self._rollback, conn)

    async def _call(
        self, sess: _Session, fn: Callable[..., Any], *args: Any, timeout: float, **kw: Any
    ) -> Any:
        """Run blocking driver work with a deadline; on timeout or cancel, stop the query."""
        task = asyncio.ensure_future(asyncio.to_thread(lambda: fn(*args, **kw)))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # The statement is still running server-side: ask it to stop, then drop the
            # connection so nothing is left half-finished or leaked.
            await asyncio.shield(self._drop(sess, rollback=False, abort=True))
            raise

    def _limits(self, sess: _Session, params: dict[str, Any]) -> ResultLimits:
        base = sess.profile.limits
        rows = params.get("max_rows")
        if isinstance(rows, int) and not isinstance(rows, bool) and 1 <= rows < base.max_rows:
            return ResultLimits(
                max_rows=rows,
                max_cell_chars=base.max_cell_chars,
                max_chars=base.max_chars,
                redact_columns=base.redact_columns,
            )
        return base

    @staticmethod
    def _catalog_limits(sess: _Session, rows: bool | int = True) -> ResultLimits:
        """Catalog results are fixed-shape metadata: page-sized, not column-redacted."""
        base = sess.profile.limits
        return ResultLimits(
            max_rows=rows if isinstance(rows, int) and rows > 1 else _MAX_PAGE + 1,
            max_cell_chars=base.max_cell_chars,
            max_chars=base.max_chars,
            default_redaction=False,
        )

    def _timeout_for(self, sess: _Session, params: dict[str, Any]) -> float:
        cap = sess.profile.statement_timeout
        asked = params.get("timeout_seconds")
        if isinstance(asked, (int, float)) and not isinstance(asked, bool) and 0 < asked < cap:
            return float(asked)
        return cap

    @staticmethod
    def _bind_values(params: dict[str, Any]) -> tuple[list[Any] | None, str]:
        raw = params.get("params")
        if raw is None or raw == []:
            return None, ""
        if not isinstance(raw, list):
            return None, "'params' must be an array of values."
        if len(raw) > _MAX_PARAMS:
            return None, f"At most {_MAX_PARAMS} bound values are allowed."
        for v in raw:
            if not _scalar(v) or (isinstance(v, str) and len(v) > _MAX_PARAM_CHARS):
                return None, "Bound values must be strings, numbers, booleans or null."
        return list(raw), ""

    def _execute_sql(
        self,
        conn: Any,
        sql: str,
        params: list[Any] | None = None,
        *,
        limits: ResultLimits | None = None,
        stream: bool = False,
    ) -> RawResult:
        """Execute one statement on *conn* (a worker thread). Fetch is bounded."""
        cur = self._open_cursor(conn, stream=stream)
        try:
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            rowcount = getattr(cur, "rowcount", -1)
            if cur.description is None:
                return RawResult([], [], rowcount if isinstance(rowcount, int) else -1)
            columns = [d[0] for d in cur.description]
            if limits is not None:
                fetched = cur.fetchmany(limits.max_rows + 1)
            else:
                fetched = cur.fetchall()
            return RawResult(
                columns, [list(r) for r in fetched], rowcount if isinstance(rowcount, int) else -1
            )
        finally:
            self._quiet(cur.close)

    def _oneshot(
        self,
        conn: Any,
        sql: str,
        params: list[Any] | None,
        *,
        read_only: bool,
        timeout_ms: int,
        limits: ResultLimits,
        stream: bool,
    ) -> RawResult:
        """Begin, run, and finish one statement in a single worker-thread call."""
        try:
            self._begin(conn, read_only=read_only, timeout_ms=timeout_ms, rows=limits.max_rows)
            raw = self._execute_sql(conn, sql, params, limits=limits, stream=stream)
            if read_only:
                conn.rollback()  # reads never commit: the snapshot ends here
            else:
                conn.commit()
            return raw
        except BaseException:
            self._quiet(self._rollback, conn)
            raise

    def _in_tx(
        self,
        conn: Any,
        sql: str,
        params: list[Any] | None,
        *,
        limits: ResultLimits,
        stream: bool,
    ) -> RawResult:
        return self._execute_sql(conn, sql, params, limits=limits, stream=stream)

    @staticmethod
    def _streamable(analysis: SQLAnalysis) -> bool:
        return analysis.read_only and analysis.verb in ("select", "with", "values", "table")

    def _fail(self, err: DbError, action: str, *, extra_hint: str = "") -> ResourceResult:
        return ResourceResult.failure(
            err.message, action=action, code=err.code, hint=(extra_hint or err.hint)
        )

    async def _run(
        self,
        sql: str,
        *,
        action: str,
        params: dict[str, Any] | None = None,
        connection: Any = None,
        allow_write: bool = False,
        retry_read: bool = True,
        catalog: bool | int = False,
    ) -> ResourceResult:
        """Run one statement, safely, on a named connection (outside any transaction)."""
        p = params or {}
        bound, problem = self._bind_values(p)
        if problem:
            return ResourceResult.failure(problem, action=action, code=ErrorCode.VALIDATION_FAILED)
        analysis = self._dialect_analysis(sql)
        try:
            require_single_statement(analysis)
        except StatementError as exc:
            return ResourceResult.failure(str(exc), action=action, code=ErrorCode.VALIDATION_FAILED)
        refusal = self._refuse(analysis, allow_write=allow_write, action=action)
        if refusal is not None:
            return refusal
        sess, failure = await self._ready(
            connection if connection is not None else p.get("connection")
        )
        if sess is None:
            assert failure is not None
            failure.action = action
            return failure
        if analysis.category is not Category.READ and sess.profile.read_only:
            return ResourceResult.denied_result(
                action, f"Connection '{sess.profile.name}' is configured read-only."
            )
        read_only = analysis.category is Category.READ
        attempts = 2 if (read_only and retry_read) else 1
        for attempt in range(attempts):
            started = time.monotonic()
            async with sess.lock:
                if sess.tx is not None:
                    return ResourceResult.failure(
                        f"Transaction {sess.tx.id} is open on connection '{sess.profile.name}'; "
                        "finish it with transaction_commit or transaction_rollback first.",
                        action=action,
                        code=ErrorCode.CONFLICT,
                    )
                if sess.conn is None or sess.state is not ResourceState.CONNECTED:
                    # Another task released the connection after readiness was checked.
                    reopened, why = await self._ready_locked(sess)
                    if not reopened:
                        assert why is not None
                        why.action = action
                        return why
                limits = self._catalog_limits(sess, catalog) if catalog else self._limits(sess, p)
                timeout = self._timeout_for(sess, p)
                try:
                    raw = await self._call(
                        sess,
                        self._oneshot,
                        sess.conn,
                        sql,
                        bound,
                        read_only=read_only,
                        timeout_ms=int(timeout * 1000),
                        limits=limits,
                        stream=self._streamable(analysis),
                        timeout=timeout + self._CLIENT_GRACE,
                    )
                except asyncio.TimeoutError:
                    note = (
                        ""
                        if read_only
                        else " The statement may or may not have been applied; verify before retrying."
                    )
                    return ResourceResult.failure(
                        f"The statement did not finish within {timeout:g}s and was cancelled.{note}",
                        action=action,
                        code=ErrorCode.TIMEOUT,
                        hint="Narrow the query, add a WHERE or LIMIT, or raise the statement timeout.",
                    )
                except Exception as exc:
                    err = classify_db_error(exc)
                    if err.connection_lost:
                        await self._drop(sess, rollback=False)
                        sess.state, sess.error = ResourceState.ERROR, err.message
                        if attempt + 1 < attempts:
                            ready, why = await self._ready_locked(sess)
                            if ready:
                                continue
                            assert why is not None
                            return why
                        note = (
                            ""
                            if read_only
                            else " Outcome unknown: the connection dropped; verify before retrying."
                        )
                        return ResourceResult.failure(
                            err.message + note, action=action, code=err.code, hint=err.hint
                        )
                    return self._fail(err, action)
            return self._finish(raw, sess, limits, action, analysis, started)
        return ResourceResult.failure("The query could not be run.", action=action)

    async def _ready_locked(self, sess: _Session) -> tuple[bool, ResourceResult | None]:
        """Reconnect a dropped session (the caller holds its lock)."""
        result = await self._connect_locked(sess)
        return (True, None) if result.ok else (False, result)

    def _refuse(
        self, analysis: SQLAnalysis, *, allow_write: bool, action: str
    ) -> ResourceResult | None:
        if analysis.category is Category.BLOCKED:
            return ResourceResult.denied_result(
                action, f"This statement is never run through the integration: {analysis.reason}."
            )
        if not allow_write and analysis.category is not Category.READ:
            return ResourceResult.failure(
                f"{action} is read-only ({analysis.reason}); use execute for statements that change data.",
                action=action,
                code=ErrorCode.UNSUPPORTED_OPERATION,
            )
        return None

    def _finish(
        self,
        raw: RawResult,
        sess: _Session,
        limits: ResultLimits,
        action: str,
        analysis: SQLAnalysis,
        started: float,
    ) -> ResourceResult:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        if raw.columns or not analysis.verb or analysis.read_only:
            shaped = shape_rows(raw.columns, lambda n, rows=raw.rows: rows[:n], limits)
            data = shaped.to_data()
        else:
            data = {"columns": [], "rows": [], "rowcount": 0, "truncated": False}
        if raw.rowcount is not None and raw.rowcount >= 0 and not raw.columns:
            data["rows_affected"] = raw.rowcount
        data["statement"] = analysis.verb or "unknown"
        data["connection"] = sess.profile.name
        data["duration_ms"] = elapsed
        warnings: list[str] = []
        if data.get("truncated"):
            warnings.append(str(data.get("truncated_reason") or "result was truncated"))
        result = ResourceResult.success(action, data)
        result.warnings = warnings
        return result

    # ── Operations: connections ──────────────────────────────────────────────

    async def _do_list_connections(self, params: dict[str, Any]) -> ResourceResult:
        rows = []
        for name in self.connection_names():
            profile, reason = self._build_profile(name)
            if profile is None:
                rows.append({"name": name, "configured": False, "problem": reason[:160]})
                continue
            sess = self._sessions.get(name)
            entry = profile.summary()
            entry["state"] = (sess.state if sess else ResourceState.DISCONNECTED).value
            entry["default"] = name == self._default_name(self.connection_names())
            rows.append(entry)
        return ResourceResult.success(
            "list_connections", {"connections": rows, "engine": self.resource_id}
        )

    async def _do_health(self, params: dict[str, Any]) -> ResourceResult:
        started = time.monotonic()
        profile, _ = self._profile_for(params.get("connection"))
        busy = self._sessions.get(profile.name) if profile is not None else None
        if busy is not None and busy.tx is not None and busy.state is ResourceState.CONNECTED:
            # A transaction owns the connection: report it alive without disturbing it.
            return ResourceResult.success(
                "health",
                {
                    "ok": True,
                    "connection": busy.profile.name,
                    "transaction_open": busy.tx.id,
                    "latency_ms": 0.0,
                },
            )
        result = await self._run("SELECT 1", action="health", params=params, retry_read=True)
        if result.ok:
            result.data = {
                "ok": True,
                "connection": result.data.get("connection"),
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "columns": result.data.get("columns"),
                "rows": result.data.get("rows"),
                "rowcount": result.data.get("rowcount"),
            }
        return result

    async def _do_diagnose(self, params: dict[str, Any]) -> ResourceResult:
        """Walk the connection path and say where it stops. Never reveals credentials."""
        steps: list[dict[str, Any]] = []

        def step(name: str, ok: bool, detail: str = "", hint: str = "") -> bool:
            entry: dict[str, Any] = {"step": name, "ok": ok}
            if detail:
                entry["detail"] = detail
            if hint and not ok:
                entry["hint"] = hint
            steps.append(entry)
            return ok

        if not step(
            "driver",
            self.driver_available(),
            self.driver_name,
            f"Install the driver ({self.install_hint or self.driver_name}).",
        ):
            return ResourceResult.success("diagnose", {"healthy": False, "steps": steps})
        profile, failure = self._profile_for(params.get("connection"))
        if not step(
            "configuration",
            profile is not None,
            (failure.error or "")[:200] if failure else "",
            "Configure it with /resource configure.",
        ):
            return ResourceResult.success("diagnose", {"healthy": False, "steps": steps})
        assert profile is not None
        invalid = self._config_problem(profile.config)
        if not step(
            "validation",
            invalid is None,
            invalid or "host, port and TLS mode look valid",
            f"Store it again with /resource configure {self.resource_id} {profile.name}.",
        ):
            return ResourceResult.success("diagnose", {"healthy": False, "steps": steps})
        problem = self._policy_problem(profile)
        if not step(
            "policy",
            problem is None,
            problem or "local host or TLS-protected remote",
            "Use a local host, or store the connection with allow_remote and sslmode=require.",
        ):
            return ResourceResult.success("diagnose", {"healthy": False, "steps": steps})
        host = str(profile.config.get("host") or "localhost")
        if not host.startswith("/"):
            try:
                port = int(profile.config.get("port") or self.default_port)
                _r, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0
                )
                writer.close()
                step("reachable", True, f"{host}:{port}")
            except (OSError, asyncio.TimeoutError, ValueError) as exc:
                step(
                    "reachable",
                    False,
                    f"{host}: {type(exc).__name__}",
                    "Is the server running and listening on that port?",
                )
                return ResourceResult.success("diagnose", {"healthy": False, "steps": steps})
        sess = self._session(profile)
        async with sess.lock:
            await self._drop(sess, rollback=False)
            result = await self._connect_locked(sess)
        step("login", result.ok, (result.error or "")[:200], result.hint)
        if not result.ok:
            return ResourceResult.success("diagnose", {"healthy": False, "steps": steps})
        probe = await self._run("SELECT 1", action="diagnose", params=params)
        step("query", probe.ok, (probe.error or "")[:200], probe.hint)
        return ResourceResult.success(
            "diagnose", {"healthy": probe.ok, "steps": steps, "connection": profile.name}
        )

    # ── Operations: running SQL ──────────────────────────────────────────────

    def _need_sql(self, params: dict[str, Any], action: str) -> ResourceResult | None:
        sql = params.get("sql") or params.get("query")
        if not isinstance(sql, str) or not sql.strip():
            return ResourceResult.failure(
                f"{action} requires a 'sql' string.",
                action=action,
                code=ErrorCode.VALIDATION_FAILED,
            )
        return None

    async def _do_query(self, params: dict[str, Any]) -> ResourceResult:
        if (bad := self._need_sql(params, "query")) is not None:
            return bad
        return await self._run(self._sql_text("query", params), action="query", params=params)

    async def _do_explain(self, params: dict[str, Any]) -> ResourceResult:
        if (bad := self._need_sql(params, "explain")) is not None:
            return bad
        return await self._run(self._sql_text("explain", params), action="explain", params=params)

    async def _do_execute(self, params: dict[str, Any]) -> ResourceResult:
        if (bad := self._need_sql(params, "execute")) is not None:
            return bad
        return await self._run(
            self._sql_text("execute", params), action="execute", params=params, allow_write=True
        )

    # ── Operations: transactions ─────────────────────────────────────────────

    async def _do_transaction_begin(self, params: dict[str, Any]) -> ResourceResult:
        action = "transaction_begin"
        read_only = bool(params.get("read_only"))
        sess, failure = await self._ready(params.get("connection"))
        if sess is None:
            assert failure is not None
            failure.action = action
            return failure
        if not read_only and sess.profile.read_only:
            return ResourceResult.denied_result(
                action, f"Connection '{sess.profile.name}' is configured read-only."
            )
        async with sess.lock:
            if sess.tx is not None:
                return ResourceResult.failure(
                    f"Transaction {sess.tx.id} is already open on '{sess.profile.name}'.",
                    action=action,
                    code=ErrorCode.CONFLICT,
                    hint="Commit or roll it back first.",
                )
            if sess.conn is None or sess.state is not ResourceState.CONNECTED:
                reopened, why = await self._ready_locked(sess)
                if not reopened:
                    assert why is not None
                    why.action = action
                    return why
            timeout = sess.profile.statement_timeout
            try:
                await self._call(
                    sess,
                    self._begin,
                    sess.conn,
                    read_only=read_only,
                    timeout_ms=int(timeout * 1000),
                    rows=sess.profile.limits.max_rows,
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return ResourceResult.failure(
                    "Starting the transaction timed out.", action=action, code=ErrorCode.TIMEOUT
                )
            except Exception as exc:
                err = classify_db_error(exc)
                if err.connection_lost:
                    await self._drop(sess, rollback=False)
                    sess.state, sess.error = ResourceState.ERROR, err.message
                return self._fail(err, action)
            tx = _Tx(uuid.uuid4().hex[:12], read_only, time.monotonic())
            sess.tx = tx
            self._tx_by_id[tx.id] = sess.profile.name
            self._arm_timer(sess)
        return ResourceResult.success(
            action,
            {
                "transaction_id": tx.id,
                "connection": sess.profile.name,
                "read_only": read_only,
                "idle_timeout_seconds": sess.profile.idle_transaction_timeout,
                "note": "Nothing is saved until transaction_commit. It rolls back if idle, on failure to commit, or at disconnect.",
            },
        )

    def _find_tx(
        self, params: dict[str, Any], action: str
    ) -> tuple[_Session | None, ResourceResult | None]:
        tid = params.get("transaction_id")
        if not isinstance(tid, str) or not tid:
            return None, ResourceResult.failure(
                f"{action} requires a 'transaction_id'.",
                action=action,
                code=ErrorCode.VALIDATION_FAILED,
            )
        name = self._tx_by_id.get(tid)
        sess = self._sessions.get(name or "")
        if sess is None or sess.tx is None or sess.tx.id != tid:
            return None, ResourceResult.failure(
                "That transaction is not open (it may have been committed, rolled back, "
                "expired while idle, or lost with its connection). Nothing was committed.",
                action=action,
                code=ErrorCode.NOT_FOUND,
                hint="Start a new one with transaction_begin.",
            )
        return sess, None

    async def _do_transaction_execute(self, params: dict[str, Any]) -> ResourceResult:
        action = "transaction_execute"
        sess, failure = self._find_tx(params, action)
        if sess is None:
            assert failure is not None
            return failure
        if (bad := self._need_sql(params, action)) is not None:
            return bad
        bound, problem = self._bind_values(params)
        if problem:
            return ResourceResult.failure(problem, action=action, code=ErrorCode.VALIDATION_FAILED)
        sql = self._sql_text("transaction_execute", params)
        analysis = self._dialect_analysis(sql)
        try:
            require_single_statement(analysis)
        except StatementError as exc:
            return ResourceResult.failure(str(exc), action=action, code=ErrorCode.VALIDATION_FAILED)
        refusal = self._refuse(analysis, allow_write=True, action=action)
        if refusal is not None:
            return refusal
        started = time.monotonic()
        async with sess.lock:
            tx = sess.tx
            if tx is None:
                return ResourceResult.failure(
                    "The transaction ended before the statement ran. Nothing was committed.",
                    action=action,
                    code=ErrorCode.NOT_FOUND,
                )
            if tx.failed:
                return ResourceResult.failure(
                    "An earlier statement in this transaction failed; only transaction_rollback is possible.",
                    action=action,
                    code=ErrorCode.CONFLICT,
                )
            if analysis.category is not Category.READ and tx.read_only:
                return ResourceResult.denied_result(action, "This transaction is read-only.")
            self._cancel_timer(sess)
            limits = self._limits(sess, params)
            timeout = self._timeout_for(sess, params)
            try:
                raw = await self._call(
                    sess,
                    self._in_tx,
                    sess.conn,
                    sql,
                    bound,
                    limits=limits,
                    stream=False,
                    timeout=timeout + self._CLIENT_GRACE,
                )
            except asyncio.TimeoutError:
                return ResourceResult.failure(
                    f"The statement did not finish within {timeout:g}s; it was cancelled and the "
                    "transaction was rolled back (nothing was committed).",
                    action=action,
                    code=ErrorCode.TIMEOUT,
                )
            except Exception as exc:
                err = classify_db_error(exc)
                if err.connection_lost:
                    await self._drop(sess, rollback=False)
                    sess.state, sess.error = ResourceState.ERROR, err.message
                    return ResourceResult.failure(
                        err.message
                        + " The transaction was lost with the connection; nothing was committed.",
                        action=action,
                        code=err.code,
                        hint="Start a new transaction after the connection recovers.",
                    )
                tx.failed = True  # the server aborts the transaction; commit must not follow
                self._arm_timer(sess)
                return self._fail(
                    err,
                    action,
                    extra_hint="The transaction is now failed: call transaction_rollback.",
                )
            tx.statements += 1
            self._arm_timer(sess)
        result = self._finish(raw, sess, limits, action, analysis, started)
        if isinstance(result.data, dict):
            result.data["transaction_id"] = tx.id
            result.data["statements_in_transaction"] = tx.statements
        return result

    async def _do_transaction_commit(self, params: dict[str, Any]) -> ResourceResult:
        action = "transaction_commit"
        sess, failure = self._find_tx(params, action)
        if sess is None:
            assert failure is not None
            return failure
        async with sess.lock:
            tx = sess.tx
            if tx is None:
                return ResourceResult.failure(
                    "The transaction already ended.", action=action, code=ErrorCode.NOT_FOUND
                )
            if tx.failed:
                await self._drop_tx(sess, reason="failed")
                return ResourceResult.failure(
                    "Refusing to commit: an earlier statement failed. The transaction was rolled back; "
                    "nothing was committed.",
                    action=action,
                    code=ErrorCode.CONFLICT,
                )
            self._cancel_timer(sess)
            conn = sess.conn
            try:
                await self._call(
                    sess, lambda: conn.commit(), timeout=sess.profile.statement_timeout
                )
            except asyncio.TimeoutError:
                return ResourceResult.failure(
                    "The commit did not finish in time and the connection was dropped. "
                    "Its outcome is UNKNOWN: verify the data before retrying.",
                    action=action,
                    code=ErrorCode.TIMEOUT,
                )
            except Exception as exc:
                err = classify_db_error(exc)
                await self._drop(sess, rollback=err.connection_lost is False)
                sess.state, sess.error = ResourceState.ERROR, err.message
                outcome = (
                    "The connection dropped during the commit; its outcome is UNKNOWN: verify the data before retrying."
                    if err.connection_lost
                    else "The commit failed and the transaction was rolled back."
                )
                return ResourceResult.failure(
                    f"{err.message} {outcome}", action=action, code=err.code, hint=err.hint
                )
            statements = tx.statements
            self._tx_by_id.pop(tx.id, None)
            sess.tx = None
        return ResourceResult.success(
            action,
            {
                "committed": True,
                "transaction_id": tx.id,
                "statements": statements,
                "connection": sess.profile.name,
            },
        )

    async def _do_transaction_rollback(self, params: dict[str, Any]) -> ResourceResult:
        action = "transaction_rollback"
        sess, failure = self._find_tx(params, action)
        if sess is None:
            assert failure is not None
            return failure
        async with sess.lock:
            tx = sess.tx
            statements = tx.statements if tx else 0
            tid = tx.id if tx else ""
            await self._drop_tx(sess, reason="requested")
        return ResourceResult.success(
            action, {"rolled_back": True, "transaction_id": tid, "statements_discarded": statements}
        )

    # ── Operations: catalog (privilege-filtered, bounded, bound parameters) ──

    def _validate_identifier(self, name: str) -> bool:
        """True if *name* is a safe SQL identifier (letters/digits/underscore/$)."""
        return bool(name) and bool(_IDENTIFIER.fullmatch(name))

    def _ident(
        self, params: dict[str, Any], key: str, action: str, *, required: bool = False
    ) -> tuple[str | None, ResourceResult | None]:
        value = params.get(key)
        if value is None or value == "":
            if required:
                return None, ResourceResult.failure(
                    f"{action} requires a valid '{key}' identifier.",
                    action=action,
                    code=ErrorCode.VALIDATION_FAILED,
                )
            return None, None
        if not isinstance(value, str) or not self._validate_identifier(value):
            return None, ResourceResult.failure(
                f"Invalid {key} name: use letters, digits, '_' or '$' (a valid identifier).",
                action=action,
                code=ErrorCode.VALIDATION_FAILED,
            )
        return value, None

    @staticmethod
    def _page(params: dict[str, Any]) -> tuple[int, int]:
        limit = params.get("limit")
        offset = params.get("offset")
        limit = (
            limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 50
        )
        offset = (
            offset if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0 else 0
        )
        return min(limit, _MAX_PAGE), offset

    async def _catalog(
        self, op: str, params: dict[str, Any], args: dict[str, Any]
    ) -> tuple[list[str], list[list[Any]], ResourceResult | None]:
        sql, binds = self._schema_sql(op, args)
        result = await self._run(
            sql,
            action=op,
            params={"connection": params.get("connection"), "params": binds},
            catalog=True,
        )
        if not result.ok:
            return [], [], result
        data = result.data
        return list(data["columns"]), [list(r) for r in data["rows"]], None

    async def _do_list_databases(self, params: dict[str, Any]) -> ResourceResult:
        return await self._run_catalog_rows("list_databases", "databases", params)

    async def _run_catalog_rows(
        self, action: str, op: str, params: dict[str, Any]
    ) -> ResourceResult:
        """A catalog op returned as the shaped rows themselves (columns + rows)."""
        sql, binds = self._schema_sql(op, dict(params))
        return await self._run(
            sql,
            action=action,
            params={"connection": params.get("connection"), "params": binds},
            catalog=True,
        )

    async def _do_database_info(self, params: dict[str, Any]) -> ResourceResult:
        cols, rows, failure = await self._catalog("info", params, {})
        if failure is not None:
            failure.action = "database_info"
            return failure
        info = dict(zip(cols, (rows[0] if rows else []), strict=False))
        return ResourceResult.success(
            "database_info", {"connection": self._conn_name(params), **info}
        )

    def _conn_name(self, params: dict[str, Any]) -> str:
        profile, _ = self._profile_for(params.get("connection"))
        return profile.name if profile else ""

    def _paged(
        self, rows: list[list[Any]], limit: int, key: str, cols: list[str]
    ) -> dict[str, Any]:
        more = len(rows) > limit
        items = [dict(zip(cols, r, strict=False)) for r in rows[:limit]]
        out: dict[str, Any] = {key: items, "count": len(items), "truncated": more}
        return out

    async def _do_list_schemas(self, params: dict[str, Any]) -> ResourceResult:
        return await self._run_catalog_rows("list_schemas", "schemas", params)

    async def _list_objects(self, action: str, params: dict[str, Any], kind: str) -> ResourceResult:
        schema, bad = self._ident(params, self._schema_key(), action)
        if bad is not None:
            return bad
        pattern = params.get("name_like")
        if pattern is not None and (not isinstance(pattern, str) or len(pattern) > 100):
            return ResourceResult.failure(
                "'name_like' must be a short string.",
                action=action,
                code=ErrorCode.VALIDATION_FAILED,
            )
        limit, offset = self._page(params)
        args = {
            "schema": schema,
            "kind": kind,
            "name_like": pattern,
            "limit": limit + 1,
            "offset": offset,
        }
        cols, rows, failure = await self._catalog("objects", params, args)
        if failure is not None:
            failure.action = action
            return failure
        key = "views" if kind == "view" else "tables"
        data = self._paged(rows, limit, key, cols)
        data.update({"connection": self._conn_name(params), "offset": offset})
        if data["truncated"]:
            data["next_offset"] = offset + limit
        return ResourceResult.success(action, data)

    def _schema_key(self) -> str:
        return "schema"

    async def _do_list_tables(self, params: dict[str, Any]) -> ResourceResult:
        return await self._list_objects("list_tables", params, "table")

    async def _do_list_views(self, params: dict[str, Any]) -> ResourceResult:
        return await self._list_objects("list_views", params, "view")

    async def _table_args(
        self, action: str, params: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, ResourceResult | None]:
        table, bad = self._ident(params, "table", action, required=True)
        if bad is not None:
            return None, bad
        schema, bad = self._ident(params, self._schema_key(), action)
        if bad is not None:
            return None, bad
        return {"table": table, "schema": schema}, None

    async def _do_describe_table(self, params: dict[str, Any]) -> ResourceResult:
        action = "describe_table"
        args, bad = await self._table_args(action, params)
        if bad is not None or args is None:
            return bad or ResourceResult.failure("invalid arguments", action=action)
        cols, rows, failure = await self._catalog("columns", params, args)
        if failure is not None:
            failure.action = action
            return failure
        if not rows:
            return ResourceResult.failure(
                f"Table '{args['table']}' was not found, or this connection cannot see it.",
                action=action,
                code=ErrorCode.NOT_FOUND,
                hint="Use list_tables to see the tables this connection can access.",
            )
        from velune.core.redaction import redact_secrets

        columns = [dict(zip(cols, r, strict=False)) for r in rows[:_MAX_PAGE]]
        for c in columns:
            if isinstance(c.get("default"), str):
                c["default"] = redact_secrets(c["default"])[:120]
        data: dict[str, Any] = {
            "connection": self._conn_name(params),
            "table": args["table"],
            "schema": args["schema"],
            "columns": columns,
            "column_count": len(rows),
            "truncated": len(rows) > _MAX_PAGE,
        }
        ccols, crows, cfail = await self._catalog("constraints", params, args)
        if cfail is None:
            constraints = self._shape_constraints(ccols, crows)
            data["primary_key"] = next(
                (c["columns"] for c in constraints if c["type"] == "primary_key"), []
            )
            data["foreign_keys"] = [c for c in constraints if c["type"] == "foreign_key"]
        return ResourceResult.success(action, data)

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [p for p in str(value).split(",") if p]

    def _shape_constraints(self, cols: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
        kinds = {
            "p": "primary_key",
            "PRIMARY KEY": "primary_key",
            "u": "unique",
            "UNIQUE": "unique",
            "f": "foreign_key",
            "FOREIGN KEY": "foreign_key",
            "c": "check",
            "CHECK": "check",
            "x": "exclusion",
        }
        out: list[dict[str, Any]] = []
        for r in rows[:_MAX_PAGE]:
            d = dict(zip(cols, r, strict=False))
            item: dict[str, Any] = {
                "name": d.get("name"),
                "type": kinds.get(str(d.get("type")), str(d.get("type")).lower()),
                "columns": self._as_list(d.get("columns")),
            }
            if d.get("ref_table"):
                item["references"] = {
                    "schema": d.get("ref_schema"),
                    "table": d.get("ref_table"),
                    "columns": self._as_list(d.get("ref_columns")),
                }
            if d.get("definition"):
                item["definition"] = str(d["definition"])[:300]
            out.append(item)
        return out

    async def _do_list_constraints(self, params: dict[str, Any]) -> ResourceResult:
        action = "list_constraints"
        args, bad = await self._table_args(action, params)
        if bad is not None or args is None:
            return bad or ResourceResult.failure("invalid arguments", action=action)
        cols, rows, failure = await self._catalog("constraints", params, args)
        if failure is not None:
            failure.action = action
            return failure
        return ResourceResult.success(
            action,
            {
                "connection": self._conn_name(params),
                "table": args["table"],
                "constraints": self._shape_constraints(cols, rows),
            },
        )

    async def _do_list_indexes(self, params: dict[str, Any]) -> ResourceResult:
        action = "list_indexes"
        args, bad = await self._table_args(action, params)
        if bad is not None or args is None:
            return bad or ResourceResult.failure("invalid arguments", action=action)
        cols, rows, failure = await self._catalog("indexes", params, args)
        if failure is not None:
            failure.action = action
            return failure
        indexes = []
        for r in rows[:_MAX_PAGE]:
            d = dict(zip(cols, r, strict=False))
            d["columns"] = self._as_list(d.get("columns"))
            if d.get("definition"):
                d["definition"] = str(d["definition"])[:300]
            indexes.append(d)
        return ResourceResult.success(
            action,
            {"connection": self._conn_name(params), "table": args["table"], "indexes": indexes},
        )

    async def _do_table_relationships(self, params: dict[str, Any]) -> ResourceResult:
        action = "table_relationships"
        args, bad = await self._table_args(action, params)
        if bad is not None or args is None:
            return bad or ResourceResult.failure("invalid arguments", action=action)
        ocols, orows, failure = await self._catalog("constraints", params, args)
        if failure is not None:
            failure.action = action
            return failure
        icols, irows, ifail = await self._catalog("referenced_by", params, args)
        outgoing = [c for c in self._shape_constraints(ocols, orows) if c["type"] == "foreign_key"]
        incoming: list[dict[str, Any]] = []
        if ifail is None:
            for r in irows[:_MAX_PAGE]:
                d = dict(zip(icols, r, strict=False))
                incoming.append(
                    {
                        "name": d.get("name"),
                        "from_schema": d.get("from_schema"),
                        "from_table": d.get("from_table"),
                        "from_columns": self._as_list(d.get("columns")),
                        "to_columns": self._as_list(d.get("ref_columns")),
                    }
                )
        return ResourceResult.success(
            action,
            {
                "connection": self._conn_name(params),
                "table": args["table"],
                "references": outgoing,
                "referenced_by": incoming,
            },
        )

    def redacted_config(self) -> dict[str, Any] | None:
        """The default connection's stored config with secrets masked — safe to display."""
        config = self._resolve_config()
        return redact_config(config) if config else None


# ── helpers ──────────────────────────────────────────────────────────────────


__all__ = [
    "DbError",
    "Profile",
    "RawResult",
    "SQLConnector",
    "classify_db_error",
    "rank",
    "sanitize_text",
]
