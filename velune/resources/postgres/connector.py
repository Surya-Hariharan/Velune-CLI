"""Local PostgreSQL connector.

Supports local instances only (localhost / 127.0.0.1 / unix socket), backed by
the ``psycopg`` (v3) driver when available. Credentials come from the encrypted
keystore; nothing here stores or logs a password in plaintext.

Permission model (enforced by the manager via ``effective_permission``):
    SELECT / EXPLAIN / introspection → READ  (auto-approved)
    INSERT/UPDATE/DELETE/DDL         → WRITE (confirmation required)
    DROP / TRUNCATE                  → ADMIN (explicit confirmation)
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


class PostgresConnector(SQLConnector):
    """Connector for a local PostgreSQL server."""

    resource_id = "postgres"
    display_name = "PostgreSQL"
    default_port = 5432
    driver_name = "psycopg (or psycopg2)"

    # ── Driver hooks ─────────────────────────────────────────────────────────

    def driver_available(self) -> bool:
        try:
            import psycopg  # noqa: F401

            return True
        except ImportError:
            try:
                import psycopg2  # pyright: ignore[reportMissingModuleSource]  # noqa: F401

                return True
            except ImportError:
                return False

    def _open_connection(self, config: dict[str, Any]) -> Any:
        params = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", self.default_port)),
            "dbname": config.get("database", "postgres"),
            "user": config.get("username", ""),
            "password": config.get("password", ""),
        }
        sslmode = config.get("sslmode") or config.get("ssl_mode")
        if sslmode:
            params["sslmode"] = sslmode
        # Prefer psycopg 3; fall back to psycopg2. Both accept these kwargs.
        try:
            import psycopg

            return psycopg.connect(**params, connect_timeout=int(config.get("timeout", 5)))
        except ImportError:
            import psycopg2  # pyright: ignore[reportMissingModuleSource]

            return psycopg2.connect(**params, connect_timeout=int(config.get("timeout", 5)))

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
            ResourceCapability("list_databases", read, "List databases"),
            ResourceCapability("list_schemas", read, "List schemas in the current database"),
            ResourceCapability("list_tables", read, "List tables"),
            ResourceCapability("describe_table", read, "Describe a table's columns"),
        ]

    async def _do_list_databases(self, params: dict[str, Any]) -> ResourceResult:
        return await self._run(
            "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname",
            action="list_databases",
        )

    async def _do_list_schemas(self, params: dict[str, Any]) -> ResourceResult:
        return await self._run(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY schema_name",
            action="list_schemas",
        )

    async def _do_list_tables(self, params: dict[str, Any]) -> ResourceResult:
        schema = params.get("schema")
        where = "table_schema NOT IN ('pg_catalog', 'information_schema')"
        if schema:
            if not self._validate_identifier(str(schema)):
                return ResourceResult.failure("Invalid schema name.", action="list_tables")
            where = f"table_schema = '{schema}'"
        return await self._run(
            "SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_type = 'BASE TABLE' AND {where} "
            "ORDER BY table_schema, table_name",
            action="list_tables",
        )

    async def _do_describe_table(self, params: dict[str, Any]) -> ResourceResult:
        table = params.get("table")
        if not table or not self._validate_identifier(str(table)):
            return ResourceResult.failure(
                "describe_table requires a valid 'table' identifier.", action="describe_table"
            )
        schema = params.get("schema", "public")
        if not self._validate_identifier(str(schema)):
            return ResourceResult.failure("Invalid schema name.", action="describe_table")
        return await self._run(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND table_schema = '{schema}' "
            "ORDER BY ordinal_position",
            action="describe_table",
        )

    # ── Discovery ────────────────────────────────────────────────────────────

    async def discover(self) -> list[DiscoveryHint]:
        """Probe the default local PostgreSQL port without authenticating."""
        import asyncio

        host, port = "127.0.0.1", self.default_port
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=1.0)
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
                display_name="Local PostgreSQL",
                detail=f"{host}:{port}",
                source="port-probe",
                suggested={"host": host, "port": port},
            )
        ]
