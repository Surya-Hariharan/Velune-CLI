"""Local MySQL / MariaDB connector.

Mirrors the PostgreSQL connector: local instances only, encrypted credentials,
read-only-by-default with per-statement permission escalation. Backed by the
``pymysql`` driver when available (also speaks to MariaDB, which is
wire-compatible).
"""

from __future__ import annotations

from typing import Any

from velune.resources.base import (
    DiscoveryHint,
    ResourceCapability,
    ResourcePermission,
    ResourceResult,
)
from velune.resources.sql_base import SQLConnector


class MySQLConnector(SQLConnector):
    """Connector for a local MySQL or MariaDB server."""

    resource_id = "mysql"
    display_name = "MySQL / MariaDB"
    default_port = 3306
    driver_name = "pymysql"

    # ── Driver hooks ─────────────────────────────────────────────────────────

    def driver_available(self) -> bool:
        try:
            import pymysql  # pyright: ignore[reportMissingModuleSource]  # noqa: F401

            return True
        except ImportError:
            return False

    def _open_connection(self, config: dict[str, Any]) -> Any:
        import pymysql  # pyright: ignore[reportMissingModuleSource]

        kwargs: dict[str, Any] = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", self.default_port)),
            "user": config.get("username", ""),
            "password": config.get("password", ""),
            "connect_timeout": int(config.get("timeout", 5)),
        }
        if config.get("database"):
            kwargs["database"] = config["database"]
        sslmode = config.get("sslmode") or config.get("ssl_mode")
        if sslmode and sslmode.lower() not in {"disable", "disabled", "false"}:
            kwargs["ssl"] = {"ssl": True}
        return pymysql.connect(**kwargs)

    def _execute_sql(self, conn: Any, sql: str) -> tuple[list[str], list[list[Any]]]:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            if cur.description is None:
                return [], []
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            return columns, rows
        finally:
            cur.close()

    # ── Introspection capabilities ───────────────────────────────────────────

    def _engine_capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        return [
            ResourceCapability("list_databases", read, "SHOW DATABASES"),
            ResourceCapability("list_tables", read, "SHOW TABLES"),
            ResourceCapability("describe_table", read, "DESCRIBE a table"),
        ]

    async def _do_list_databases(self, params: dict[str, Any]) -> ResourceResult:
        return await self._run("SHOW DATABASES", action="list_databases")

    async def _do_list_tables(self, params: dict[str, Any]) -> ResourceResult:
        database = params.get("database")
        if database:
            if not self._validate_identifier(str(database)):
                return ResourceResult.failure("Invalid database name.", action="list_tables")
            return await self._run(f"SHOW TABLES FROM `{database}`", action="list_tables")
        return await self._run("SHOW TABLES", action="list_tables")

    async def _do_describe_table(self, params: dict[str, Any]) -> ResourceResult:
        table = params.get("table")
        if not table or not self._validate_identifier(str(table)):
            return ResourceResult.failure(
                "describe_table requires a valid 'table' identifier.", action="describe_table"
            )
        return await self._run(f"DESCRIBE `{table}`", action="describe_table")

    # ── Discovery ────────────────────────────────────────────────────────────

    async def discover(self) -> list[DiscoveryHint]:
        """Probe the default local MySQL port without authenticating."""
        import asyncio

        host, port = "127.0.0.1", self.default_port
        try:
            fut = asyncio.open_connection(host, port)
            _reader, writer = await asyncio.wait_for(fut, timeout=1.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except (OSError, asyncio.TimeoutError):
            return []
        return [
            DiscoveryHint(
                resource_id=self.resource_id,
                display_name="Local MySQL / MariaDB",
                detail=f"{host}:{port}",
                source="port-probe",
                suggested={"host": host, "port": port},
            )
        ]
