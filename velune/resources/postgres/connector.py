"""PostgreSQL connector (VAIR A5).

Backed by ``psycopg`` (v3) when available, falling back to ``psycopg2``. Credentials
come from the encrypted keystore; nothing here stores or logs a password.

Everything shared with MySQL — named connections, single-statement classification,
bounded read-only execution, transactions, results — lives in
:class:`velune.resources.sql_base.SQLConnector`. This module supplies only what is
specific to PostgreSQL: the driver, how a transaction is started (``READ ONLY`` plus
``statement_timeout``), server-side cursors for bounded reads, query cancellation, and
the catalog SQL.

Catalog queries read ``information_schema`` and the ``pg_catalog`` tables **filtered by
the connected role's own privileges** (``has_table_privilege`` / ``has_schema_privilege``
/ ``has_database_privilege``), so discovery never lists an object the role cannot
use. Every value is a bound parameter.
"""

from __future__ import annotations

from typing import Any

from velune.resources.base import (
    DiscoveryHint,
    ResourceCapability,
    ResourcePermission,
)
from velune.resources.sql_base import SQLConnector
from velune.resources.sql_parse import Dialect

#: Any privilege on a table makes it visible to this role.
_ANY_PRIV = "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'"

_COLS_OF = (
    "(SELECT array_agg(a.attname ORDER BY k.ord) "
    "FROM unnest({keys}) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute a ON a.attrelid = {rel} AND a.attnum = k.attnum)"
)


class PostgresConnector(SQLConnector):
    """Connector for PostgreSQL (local by default; remote only with TLS)."""

    resource_id = "postgres"
    display_name = "PostgreSQL"
    description = (
        "PostgreSQL databases: inspect schemas, tables, keys and indexes; run read-only "
        "queries; make approved changes and transactions."
    )
    keywords = (
        "postgres",
        "postgresql",
        "psql",
        "database",
        "sql",
        "query",
        "table",
        "tables",
        "schema",
    )
    default_port = 5432
    driver_name = "psycopg (or psycopg2)"
    install_hint = 'pip install "psycopg[binary]"'
    dialect = Dialect.POSTGRES

    # ── Driver hooks ─────────────────────────────────────────────────────────

    def driver_available(self) -> bool:
        try:
            import psycopg  # noqa: F401

            return True
        except ImportError:
            try:
                import psycopg2  # type: ignore  # noqa: F401

                return True
            except ImportError:
                return False

    def _open_connection(self, config: dict[str, Any]) -> Any:
        params: dict[str, Any] = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", self.default_port)),
            "dbname": config.get("database", "postgres"),
            "user": config.get("username", ""),
            "password": config.get("password", ""),
            "application_name": "velune",
            # A forgotten transaction is ended by the server itself after five minutes.
            "options": "-c idle_in_transaction_session_timeout=300000",
        }
        sslmode = config.get("sslmode") or config.get("ssl_mode")
        if sslmode:
            params["sslmode"] = sslmode
        if config.get("sslrootcert"):
            params["sslrootcert"] = config["sslrootcert"]
        timeout = int(config.get("timeout", 5))
        # Prefer psycopg 3; fall back to psycopg2. Both accept these kwargs.
        try:
            import psycopg

            return psycopg.connect(**params, connect_timeout=timeout)
        except ImportError:
            import psycopg2  # type: ignore

            return psycopg2.connect(**params, connect_timeout=timeout)

    def _begin(self, conn: Any, *, read_only: bool, timeout_ms: int, rows: int) -> None:
        """Open a transaction in the requested mode, with server-side timeouts.

        ``SET TRANSACTION READ ONLY`` is the engine-level guarantee behind every read:
        even a statement the classifier mis-judged cannot change data inside it.
        """
        cur = conn.cursor()
        try:
            if read_only:
                cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(f"SET LOCAL lock_timeout = {min(int(timeout_ms), 10_000)}")
        finally:
            self._quiet(cur.close)

    def _cancel(self, conn: Any) -> None:
        conn.cancel()

    @staticmethod
    def _is_psycopg3(conn: Any) -> bool:
        """psycopg 3 reports its classes as ``psycopg`` (psycopg2's live in ``psycopg2.*``)."""
        module = type(conn).__module__
        return module == "psycopg" or module.startswith("psycopg.")

    def _open_cursor(self, conn: Any, *, stream: bool) -> Any:
        # A server-side cursor streams rows, so a huge SELECT is never loaded whole into
        # memory. psycopg 3 only: psycopg2 exposes no column description until a fetch.
        if stream and self._is_psycopg3(conn):
            return conn.cursor(name="velune_cursor")
        return conn.cursor()

    # ── Catalog SQL ──────────────────────────────────────────────────────────

    def _engine_capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        conn = {"type": "string", "description": "Named connection (see list_connections)."}
        schema = {"type": "string", "description": "Schema name (default: the current schema)."}
        paging = {
            "limit": {
                "type": "integer",
                "description": "Most items to return (default 50).",
                "minimum": 1,
                "maximum": 200,
            },
            "offset": {"type": "integer", "description": "Items to skip.", "minimum": 0},
            "name_like": {"type": "string", "description": "Only names containing this text."},
        }
        table = {"type": "string", "description": "Table name."}
        scope = "database.schema.read"
        return [
            ResourceCapability(
                "database_info",
                read,
                "Server version, current database and role, encoding and time zone",
                scope=scope,
                category="schema",
                params={"properties": {"connection": conn}},
            ),
            ResourceCapability(
                "list_databases",
                read,
                "List the databases this role can connect to",
                scope=scope,
                category="schema",
                params={"properties": {"connection": conn}},
            ),
            ResourceCapability(
                "list_schemas",
                read,
                "List the schemas this role can use",
                scope=scope,
                category="schema",
                params={"properties": {"connection": conn}},
            ),
            ResourceCapability(
                "list_tables",
                read,
                "List tables this role can see (paged)",
                scope=scope,
                category="schema",
                params={"properties": {"schema": schema, "connection": conn, **paging}},
            ),
            ResourceCapability(
                "list_views",
                read,
                "List views this role can see (paged)",
                scope=scope,
                category="schema",
                params={"properties": {"schema": schema, "connection": conn, **paging}},
            ),
            ResourceCapability(
                "describe_table",
                read,
                "A table's columns, primary key and foreign keys",
                scope=scope,
                category="schema",
                params={
                    "properties": {"table": table, "schema": schema, "connection": conn},
                    "required": ["table"],
                },
            ),
            ResourceCapability(
                "list_indexes",
                read,
                "A table's indexes",
                scope=scope,
                category="schema",
                params={
                    "properties": {"table": table, "schema": schema, "connection": conn},
                    "required": ["table"],
                },
            ),
            ResourceCapability(
                "list_constraints",
                read,
                "A table's primary key, unique, foreign key and check constraints",
                scope=scope,
                category="schema",
                params={
                    "properties": {"table": table, "schema": schema, "connection": conn},
                    "required": ["table"],
                },
            ),
            ResourceCapability(
                "table_relationships",
                read,
                "Foreign keys out of a table and the tables that reference it",
                scope=scope,
                category="schema",
                params={
                    "properties": {"table": table, "schema": schema, "connection": conn},
                    "required": ["table"],
                },
            ),
        ]

    def _schema_sql(self, op: str, args: dict[str, Any]) -> tuple[str, list[Any]]:
        schema = args.get("schema")
        table = args.get("table")
        if op == "databases":
            return (
                "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate "
                "AND has_database_privilege(datname, 'CONNECT') ORDER BY datname LIMIT 201",
                [],
            )
        if op == "schemas":
            return (
                "SELECT nspname AS schema_name FROM pg_namespace "
                "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' "
                "AND has_schema_privilege(oid, 'USAGE') ORDER BY nspname LIMIT 201",
                [],
            )
        if op == "info":
            return (
                "SELECT current_database() AS database, current_user AS role, "
                "version() AS version, current_setting('server_encoding') AS encoding, "
                "current_setting('TimeZone') AS timezone, "
                "current_setting('default_transaction_read_only') AS default_read_only",
                [],
            )
        if op == "objects":
            kind = "VIEW" if args.get("kind") == "view" else "BASE TABLE"
            sql = (
                "SELECT table_schema AS schema, table_name AS name, "
                "CASE table_type WHEN 'BASE TABLE' THEN 'table' ELSE 'view' END AS kind "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                "AND table_schema !~ '^pg_' AND table_type = %s"
            )
            binds: list[Any] = [kind]
            if schema:
                sql += " AND table_schema = %s"
                binds.append(schema)
            pattern = args.get("name_like")
            if pattern:
                escaped = str(pattern).replace("!", "!!").replace("%", "!%").replace("_", "!_")
                sql += " AND table_name ILIKE %s ESCAPE '!'"
                binds.append(f"%{escaped}%")
            sql += " ORDER BY table_schema, table_name LIMIT %s OFFSET %s"
            binds += [int(args.get("limit", 51)), int(args.get("offset", 0))]
            return sql, binds
        if op == "columns":
            return (
                "SELECT column_name AS name, "
                "CASE WHEN data_type IN ('USER-DEFINED', 'ARRAY') THEN udt_name "
                "ELSE data_type END AS type, "
                "is_nullable = 'YES' AS nullable, column_default AS default_value, "
                "ordinal_position AS position, character_maximum_length AS max_length "
                "FROM information_schema.columns "
                "WHERE table_schema = COALESCE(%s, current_schema()) AND table_name = %s "
                "ORDER BY ordinal_position LIMIT 201",
                [schema, table],
            )
        if op == "constraints":
            cols = _COLS_OF.format(keys="con.conkey", rel="con.conrelid")
            ref_cols = _COLS_OF.format(keys="con.confkey", rel="con.confrelid")
            return (
                f"SELECT con.conname AS name, con.contype::text AS type, {cols} AS columns, "  # nosec B608 - only module-level constants are interpolated
                f"refns.nspname AS ref_schema, refcl.relname AS ref_table, "
                f"{ref_cols} AS ref_columns, pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con "
                "JOIN pg_class cl ON cl.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "LEFT JOIN pg_class refcl ON refcl.oid = con.confrelid "
                "LEFT JOIN pg_namespace refns ON refns.oid = refcl.relnamespace "
                "WHERE ns.nspname = COALESCE(%s, current_schema()) AND cl.relname = %s "
                f"AND has_table_privilege(cl.oid, {_ANY_PRIV}) "
                f"AND (con.confrelid = 0 OR has_table_privilege(con.confrelid, {_ANY_PRIV})) "
                "ORDER BY con.contype, con.conname LIMIT 201",
                [schema, table],
            )
        if op == "indexes":
            cols = _COLS_OF.format(keys="ix.indkey::int2[]", rel="ix.indrelid")
            return (
                "SELECT i.relname AS name, ix.indisunique AS is_unique, "  # nosec B608 - only module-level constants are interpolated
                f"ix.indisprimary AS is_primary, am.amname AS method, {cols} AS columns, "
                "pg_get_indexdef(ix.indexrelid) AS definition "
                "FROM pg_index ix "
                "JOIN pg_class i ON i.oid = ix.indexrelid "
                "JOIN pg_class t ON t.oid = ix.indrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_am am ON am.oid = i.relam "
                "WHERE n.nspname = COALESCE(%s, current_schema()) AND t.relname = %s "
                f"AND has_table_privilege(t.oid, {_ANY_PRIV}) ORDER BY i.relname LIMIT 201",
                [schema, table],
            )
        if op == "referenced_by":
            cols = _COLS_OF.format(keys="con.conkey", rel="con.conrelid")
            ref_cols = _COLS_OF.format(keys="con.confkey", rel="con.confrelid")
            return (
                f"SELECT con.conname AS name, ns.nspname AS from_schema, cl.relname AS from_table, "  # nosec B608 - only module-level constants are interpolated
                f"{cols} AS columns, {ref_cols} AS ref_columns "
                "FROM pg_constraint con "
                "JOIN pg_class cl ON cl.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "WHERE con.contype = 'f' AND con.confrelid = ("
                "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = COALESCE(%s, current_schema()) AND c.relname = %s) "
                f"AND has_table_privilege(cl.oid, {_ANY_PRIV}) "
                f"AND has_table_privilege(con.confrelid, {_ANY_PRIV}) "
                "ORDER BY ns.nspname, cl.relname, con.conname LIMIT 201",
                [schema, table],
            )
        raise ValueError(f"unknown catalog operation {op!r}")

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
