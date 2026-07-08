"""Shared base for local SQL database connectors (PostgreSQL, MySQL/MariaDB).

Holds everything the two connectors have in common so PostgreSQL and MySQL
differ only in their driver and their introspection SQL:

- config resolution from the encrypted keystore (or an explicit dict);
- **local-only enforcement** — a remote host is refused outright, because these
  connectors exist to talk to a developer's local database, not to reach across
  the network with stored credentials;
- read-only-by-default execution with per-statement permission escalation
  (SELECT auto-approves; DDL → WRITE; DROP → ADMIN) via
  :func:`velune.resources.sql_safety.classify_sql`;
- password-safe error handling and approval previews.

Drivers (``psycopg`` / ``pymysql``) are imported lazily inside the subclass so
importing this module never pulls a database dependency, and a missing driver
degrades to a structured "driver not installed" result instead of a crash.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from abc import abstractmethod
from typing import Any

from velune.resources.base import (
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.secrets import load_resource_secret, redact_config
from velune.resources.sql_safety import classify_sql

logger = logging.getLogger("velune.resources.sql")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_PASSWORD_IN_TEXT = re.compile(r"(password|pwd)\s*=\s*\S+", re.IGNORECASE)


class SQLConnector(ResourceConnector):
    """Common lifecycle + gated SQL execution for local relational databases."""

    #: Default TCP port, overridden per engine.
    default_port: int = 5432
    #: Human name of the required driver, used in the "not installed" message.
    driver_name: str = ""

    def __init__(self, name: str | None = None, config: dict[str, Any] | None = None) -> None:
        super().__init__(name)
        # An explicit config wins; otherwise it is loaded (decrypted) on connect.
        self._explicit_config = config
        self._conn: Any = None
        self._state = ResourceState.DISCONNECTED
        self._error: str | None = None
        self._info: dict[str, Any] = {}

    # ── Driver hooks (implemented per engine) ────────────────────────────────

    @abstractmethod
    def driver_available(self) -> bool:
        """True when the engine's Python driver can be imported."""

    @abstractmethod
    def _open_connection(self, config: dict[str, Any]) -> Any:
        """Open a live driver connection (runs in a worker thread). May raise."""

    @abstractmethod
    def _execute_sql(self, conn: Any, sql: str) -> tuple[list[str], list[list[Any]]]:
        """Execute *sql*, returning ``(column_names, rows)`` (runs in a thread)."""

    @abstractmethod
    def _engine_capabilities(self) -> list[ResourceCapability]:
        """Engine-specific introspection capabilities (list_databases, …)."""

    # ── Config / locality ────────────────────────────────────────────────────

    def _resolve_config(self) -> dict[str, Any] | None:
        if self._explicit_config:
            return self._explicit_config
        return load_resource_secret(self.resource_id, self.name)

    @staticmethod
    def is_local_host(host: str | None) -> bool:
        """True only for loopback hosts or a unix-socket path.

        This is the security boundary that keeps these connectors *local*: a
        stored password is never sent to a non-loopback address.
        """
        if host is None:
            return True  # driver default (localhost / socket)
        h = host.strip()
        if not h:
            return True
        if h.startswith("/"):  # unix socket directory
            return True
        h = h.lower().strip("[]")
        if h in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            return ipaddress.ip_address(h).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _sanitize(text: str) -> str:
        """Strip any ``password=...`` fragment a driver may echo in an error."""
        return _PASSWORD_IN_TEXT.sub(r"\1=***", text)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> ResourceResult:
        if not self.driver_available():
            self._state = ResourceState.UNAVAILABLE
            self._error = (
                f"{self.driver_name} driver is not installed. "
                f"Install it to use the {self.display_name} connector."
            )
            return ResourceResult.failure(self._error, action="connect")

        config = self._resolve_config()
        if not config:
            self._state = ResourceState.DISCONNECTED
            return ResourceResult.failure(
                f"No stored configuration for {self.display_name} '{self.name}'. "
                "Configure it with host/port/database/username first.",
                action="connect",
            )

        host = config.get("host", "localhost")
        if not self.is_local_host(host):
            self._state = ResourceState.ERROR
            self._error = (
                f"Refusing to connect to non-local host '{host}'. "
                f"The {self.display_name} connector supports local instances only."
            )
            return ResourceResult.failure(self._error, action="connect")

        self._state = ResourceState.CONNECTING
        try:
            self._conn = await asyncio.to_thread(self._open_connection, config)
        except Exception as exc:
            self._state = ResourceState.ERROR
            self._error = self._sanitize(f"{type(exc).__name__}: {exc}")
            return ResourceResult.failure(self._error, action="connect")

        self._state = ResourceState.CONNECTED
        self._error = None
        self._info = {
            "host": host,
            "port": config.get("port", self.default_port),
            "database": config.get("database"),
        }
        return ResourceResult.success(action="connect", data=dict(self._info))

    async def disconnect(self) -> ResourceResult:
        if self._conn is not None:
            try:
                await asyncio.to_thread(self._conn.close)
            except Exception as exc:
                logger.debug("Error closing %s connection: %s", self.resource_id, exc)
            self._conn = None
        self._state = ResourceState.DISCONNECTED
        return ResourceResult.success(action="disconnect")

    def status(self) -> ResourceStatus:
        return ResourceStatus(
            resource_id=self.resource_id,
            display_name=self.display_name,
            state=self._state,
            detail=self._detail(),
            info=dict(self._info),
            error=self._error,
        )

    def _detail(self) -> str:
        if self._info.get("host"):
            db = self._info.get("database") or ""
            return f"{self._info['host']}:{self._info.get('port', self.default_port)}/{db}".rstrip(
                "/"
            )
        return ""

    # ── Capabilities ─────────────────────────────────────────────────────────

    def capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        return [
            *self._engine_capabilities(),
            ResourceCapability(
                "query", read, "Run SQL. SELECT auto-approves; writes/DDL need confirmation"
            ),
            ResourceCapability("explain", read, "EXPLAIN a query plan (read-only)"),
            ResourceCapability("health", read, "Check connection health"),
        ]

    def effective_permission(
        self, action: str, params: dict[str, Any] | None
    ) -> tuple[ResourcePermission, bool] | None:
        """Escalate the read-only ``query`` action based on the actual SQL."""
        if action == "query":
            sql = self._sql_param(params)
            verdict = classify_sql(sql)
            return (verdict.permission, verdict.permission is ResourcePermission.ADMIN)
        return None

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        if action in {"query", "explain"}:
            return self._sql_param(params)[:300]
        return ""

    @staticmethod
    def _sql_param(params: dict[str, Any] | None) -> str:
        params = params or {}
        return str(params.get("sql") or params.get("query") or "")

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = params or {}
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(f"Unsupported action '{action}'.", action=action)
        return await handler(params)

    async def _ensure_connected(self) -> ResourceResult | None:
        """Lazily connect if needed; return a failure result if it can't."""
        if self._conn is not None and self._state is ResourceState.CONNECTED:
            return None
        result = await self.connect()
        return None if result.ok else result

    async def _run(self, sql: str, *, action: str) -> ResourceResult:
        """Execute arbitrary SQL against the live connection, safely."""
        failure = await self._ensure_connected()
        if failure is not None:
            return failure
        try:
            columns, rows = await asyncio.to_thread(self._execute_sql, self._conn, sql)
        except Exception as exc:
            return ResourceResult.failure(
                self._sanitize(f"{type(exc).__name__}: {exc}"), action=action
            )
        return ResourceResult.success(
            action=action, data={"columns": columns, "rows": rows, "rowcount": len(rows)}
        )

    async def _do_query(self, params: dict[str, Any]) -> ResourceResult:
        sql = self._sql_param(params)
        if not sql.strip():
            return ResourceResult.failure("query requires a 'sql' string.", action="query")
        return await self._run(sql, action="query")

    async def _do_explain(self, params: dict[str, Any]) -> ResourceResult:
        sql = self._sql_param(params)
        if not sql.strip():
            return ResourceResult.failure("explain requires a 'sql' string.", action="explain")
        return await self._run(f"EXPLAIN {sql}", action="explain")

    async def _do_health(self, params: dict[str, Any]) -> ResourceResult:
        return await self._run("SELECT 1", action="health")

    def _validate_identifier(self, name: str) -> bool:
        """True if *name* is a safe SQL identifier (letters/digits/underscore)."""
        return bool(name) and bool(_IDENTIFIER.match(name))

    def redacted_config(self) -> dict[str, Any] | None:
        """The stored config with secrets masked — safe to display."""
        config = self._resolve_config()
        return redact_config(config) if config else None
