"""MySQL / MariaDB connector (VAIR A5).

Backed by ``pymysql``. Everything shared with PostgreSQL — named connections,
single-statement classification, bounded read-only execution, transactions, results —
lives in :class:`velune.resources.sql_base.SQLConnector`. This module supplies what is
specific to MySQL: the driver (with ``local_infile`` disabled), how a transaction is
started (``START TRANSACTION READ ONLY`` plus an execution-time limit), query
cancellation (``KILL QUERY`` from a second connection), and the catalog SQL.

In MySQL a *schema* is a *database*; catalog operations take ``database``. Catalog
queries read ``information_schema`` and ``SHOW``, which MySQL already filters by the
connected user's privileges.
"""

from __future__ import annotations

import contextlib
import ssl as _ssl
from typing import Any

from velune.resources.base import (
    DiscoveryHint,
    ErrorCode,
    ResourceCapability,
    ResourcePermission,
    ResourceResult,
)
from velune.resources.sql_base import SQLConnector
from velune.resources.sql_parse import Dialect

#: SHOW TABLES cannot be paged by the server, so it is read whole (bounded) and paged here.
_SHOW_ROWS = 5000


class MySQLConnector(SQLConnector):
    """Connector for MySQL or MariaDB (local by default; remote only with TLS)."""

    resource_id = "mysql"
    display_name = "MySQL / MariaDB"
    description = (
        "MySQL or MariaDB databases: inspect tables, keys and indexes; run read-only "
        "queries; make approved changes and transactions."
    )
    keywords = ("mysql", "mariadb", "database", "sql", "query", "table", "tables")
    default_port = 3306
    driver_name = "pymysql"
    install_hint = "pip install pymysql"
    dialect = Dialect.MYSQL

    # ── Driver hooks ─────────────────────────────────────────────────────────

    def driver_available(self) -> bool:
        try:
            import pymysql  # type: ignore  # noqa: F401

            return True
        except ImportError:
            return False

    def _open_connection(self, config: dict[str, Any]) -> Any:
        import pymysql  # type: ignore

        kwargs: dict[str, Any] = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", self.default_port)),
            "user": config.get("username", ""),
            "password": config.get("password", ""),
            "connect_timeout": int(config.get("timeout", 5)),
            "autocommit": False,
            # Never let the server ask this client to read local files.
            "local_infile": False,
        }
        if config.get("database"):
            kwargs["database"] = config["database"]
        mode = str(config.get("sslmode") or config.get("ssl_mode") or "").lower()
        if mode and mode not in {"disable", "disabled", "false"}:
            if mode in {"verify-ca", "verify_ca", "verify-full", "verify_identity"}:
                # A real, verifying context: the server's certificate is checked.
                ctx = _ssl.create_default_context(cafile=config.get("sslrootcert") or None)
                ctx.check_hostname = mode in {"verify-full", "verify_identity"}
                kwargs["ssl"] = ctx
            else:  # "require": encrypted, not authenticated (as in libpq)
                kwargs["ssl"] = {"ssl": True}
        conn = pymysql.connect(**kwargs)
        # What a second connection needs to cancel a running query on this one.
        with contextlib.suppress(Exception):
            conn._velune_cancel_args = {
                k: v for k, v in kwargs.items() if k not in {"autocommit", "database"}
            }
        return conn

    def _begin(self, conn: Any, *, read_only: bool, timeout_ms: int, rows: int) -> None:
        """Open a transaction in the requested mode, with execution limits.

        ``START TRANSACTION READ ONLY`` is the engine-level guarantee behind every read.
        ``sql_select_limit`` caps what a read can return (and is reset for writes, so it
        can never truncate an ``INSERT … SELECT``).
        """
        cur = conn.cursor()
        try:
            cur.execute("START TRANSACTION READ ONLY" if read_only else "START TRANSACTION")
            for stmt in (
                f"SET SESSION max_execution_time = {int(timeout_ms)}",  # MySQL 5.7.8+, ms
                f"SET SESSION max_statement_time = {int(timeout_ms) / 1000:.3f}",  # MariaDB, s
            ):
                if self._try_execute(cur, stmt):
                    break
            self._try_execute(
                cur,
                f"SET SESSION sql_select_limit = {int(rows) + 1}"
                if read_only
                else "SET SESSION sql_select_limit = DEFAULT",
            )
        finally:
            self._quiet(cur.close)

    def _cancel(self, conn: Any) -> None:
        import pymysql  # type: ignore

        args = getattr(conn, "_velune_cancel_args", None)
        if not args:
            return
        thread_id = int(conn.thread_id())
        killer = pymysql.connect(**args)
        try:
            cur = killer.cursor()
            cur.execute(f"KILL QUERY {thread_id}")
            cur.close()
        finally:
            killer.close()

    # ── Catalog SQL ──────────────────────────────────────────────────────────

    def _schema_key(self) -> str:
        return "database"

    def _engine_capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        conn = {"type": "string", "description": "Named connection (see list_connections)."}
        database = {"type": "string", "description": "Database name (default: the current one)."}
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
        by_table = {
            "properties": {"table": table, "database": database, "connection": conn},
            "required": ["table"],
        }
        return [
            ResourceCapability(
                "database_info",
                read,
                "Server version, current database and user, character set and time zone",
                scope=scope,
                category="schema",
                params={"properties": {"connection": conn}},
            ),
            ResourceCapability(
                "list_databases",
                read,
                "List the databases this user can see",
                scope=scope,
                category="schema",
                params={"properties": {"connection": conn}},
            ),
            ResourceCapability(
                "list_tables",
                read,
                "List tables this user can see (paged)",
                scope=scope,
                category="schema",
                params={"properties": {"database": database, "connection": conn, **paging}},
            ),
            ResourceCapability(
                "list_views",
                read,
                "List views this user can see (paged)",
                scope=scope,
                category="schema",
                params={"properties": {"database": database, "connection": conn, **paging}},
            ),
            ResourceCapability(
                "describe_table",
                read,
                "A table's columns, primary key and foreign keys",
                scope=scope,
                category="schema",
                params=by_table,
            ),
            ResourceCapability(
                "list_indexes",
                read,
                "A table's indexes",
                scope=scope,
                category="schema",
                params=by_table,
            ),
            ResourceCapability(
                "list_constraints",
                read,
                "A table's primary key, unique, foreign key and check constraints",
                scope=scope,
                category="schema",
                params=by_table,
            ),
            ResourceCapability(
                "table_relationships",
                read,
                "Foreign keys out of a table and the tables that reference it",
                scope=scope,
                category="schema",
                params=by_table,
            ),
        ]

    def _schema_sql(self, op: str, args: dict[str, Any]) -> tuple[str, list[Any]]:
        db = args.get("database") or args.get("schema")
        table = args.get("table")
        if op == "databases":
            return "SHOW DATABASES", []
        if op == "info":
            return (
                "SELECT DATABASE() AS db, CURRENT_USER() AS user_name, VERSION() AS version, "
                "@@character_set_server AS encoding, @@time_zone AS timezone",
                [],
            )
        if op == "objects":
            # Identifiers cannot be bound; the name is validated to letters/digits/_/$ first.
            if db:
                return f"SHOW FULL TABLES FROM `{db}`", []
            return "SHOW FULL TABLES", []
        if op == "columns":
            return (
                "SELECT column_name AS name, column_type AS type, is_nullable = 'YES' AS nullable, "
                "column_default AS default_value, ordinal_position AS position, "
                "character_maximum_length AS max_length FROM information_schema.columns "
                "WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s "
                "ORDER BY ordinal_position LIMIT 201",
                [db, table],
            )
        if op == "constraints":
            return (
                "SELECT tc.constraint_name AS name, tc.constraint_type AS type, "
                "GROUP_CONCAT(kcu.column_name ORDER BY kcu.ordinal_position) AS columns, "
                "kcu.referenced_table_schema AS ref_schema, kcu.referenced_table_name AS ref_table, "
                "GROUP_CONCAT(kcu.referenced_column_name ORDER BY kcu.ordinal_position) AS ref_columns "
                "FROM information_schema.table_constraints tc "
                "LEFT JOIN information_schema.key_column_usage kcu "
                "ON kcu.constraint_schema = tc.constraint_schema "
                "AND kcu.constraint_name = tc.constraint_name AND kcu.table_name = tc.table_name "
                "WHERE tc.table_schema = COALESCE(%s, DATABASE()) AND tc.table_name = %s "
                "GROUP BY tc.constraint_name, tc.constraint_type, "
                "kcu.referenced_table_schema, kcu.referenced_table_name "
                "ORDER BY tc.constraint_type, tc.constraint_name LIMIT 201",
                [db, table],
            )
        if op == "indexes":
            return (
                "SELECT index_name AS name, non_unique = 0 AS is_unique, "
                "index_name = 'PRIMARY' AS is_primary, index_type AS method, "
                "GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns "
                "FROM information_schema.statistics "
                "WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s "
                "GROUP BY index_name, non_unique, index_type ORDER BY index_name LIMIT 201",
                [db, table],
            )
        if op == "referenced_by":
            return (
                "SELECT constraint_name AS name, table_schema AS from_schema, "
                "table_name AS from_table, GROUP_CONCAT(column_name ORDER BY ordinal_position) "
                "AS columns, GROUP_CONCAT(referenced_column_name ORDER BY ordinal_position) "
                "AS ref_columns FROM information_schema.key_column_usage "
                "WHERE referenced_table_schema = COALESCE(%s, DATABASE()) "
                "AND referenced_table_name = %s "
                "GROUP BY constraint_name, table_schema, table_name "
                "ORDER BY table_schema, table_name, constraint_name LIMIT 201",
                [db, table],
            )
        raise ValueError(f"unknown catalog operation {op!r}")

    async def _list_objects(self, action: str, params: dict[str, Any], kind: str) -> ResourceResult:
        """``SHOW FULL TABLES`` can't be paged by the server: read it (bounded), page here."""
        database, bad = self._ident(params, "database", action)
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
        sql, binds = self._schema_sql("objects", {"database": database})
        result = await self._run(
            sql,
            action=action,
            params={"connection": params.get("connection"), "params": binds},
            catalog=_SHOW_ROWS,
        )
        if not result.ok:
            return result
        wanted = "VIEW" if kind == "view" else "BASE TABLE"
        items: list[dict[str, Any]] = []
        for row in result.data["rows"]:
            name = str(row[0])
            table_type = str(row[1]).upper() if len(row) > 1 else "BASE TABLE"
            if table_type not in {"BASE TABLE", "VIEW"}:
                table_type = "BASE TABLE"
            if table_type != wanted:
                continue
            if pattern and str(pattern).lower() not in name.lower():
                continue
            items.append(
                {
                    "schema": database,
                    "name": name,
                    "kind": "view" if table_type == "VIEW" else "table",
                }
            )
        page = items[offset : offset + limit]
        more = len(items) > offset + limit
        key = "views" if kind == "view" else "tables"
        data: dict[str, Any] = {
            "connection": result.data.get("connection"),
            key: page,
            "count": len(page),
            "offset": offset,
            "truncated": more or bool(result.data.get("truncated")),
        }
        if more:
            data["next_offset"] = offset + limit
        return ResourceResult.success(action, data)

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
