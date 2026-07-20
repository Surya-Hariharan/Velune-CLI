"""Async single-writer SQLite connection pool.

Eliminates WAL write contention by serialising all writes through one
persistent connection guarded by an ``asyncio.Lock``.  Reads open a
short-lived second connection per call; WAL mode lets concurrent readers
coexist with the single writer without blocking.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger("velune.memory.storage.sqlite_pool")

_WRITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",  # 64 MB page cache
    "PRAGMA foreign_keys=ON",
    # SQLite defaults to failing immediately (SQLITE_BUSY) on lock
    # contention. The single-writer lock already serializes writes within
    # one process, but a second `velune` process touching the same
    # workspace DB (or a reader caught mid-checkpoint) can still collide
    # briefly; retry for up to 5s instead of surfacing that as an error.
    "PRAGMA busy_timeout=5000",
)


class SQLiteConnectionPool:
    """Async single-writer SQLite connection pool.

    One persistent write connection is held open for the lifetime of the pool,
    protected by an ``asyncio.Lock`` so only one coroutine writes at a time.
    Read connections are short-lived, opened and closed per query; WAL mode
    ensures they never block behind the writer.

    Lifecycle
    ---------
    Call ``await pool.startup()`` before any read/write operations.
    Call ``await pool.shutdown()`` during graceful teardown (commits any
    pending work and closes the connection).

    Usage
    -----
    Writes::

        async with pool.write() as conn:
            await conn.execute("INSERT INTO t VALUES (?)", (val,))
            # commit happens automatically on exit

    Reads::

        async with pool.read() as conn:
            cursor = await conn.execute("SELECT * FROM t")
            rows = await cursor.fetchall()
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_conn: aiosqlite.Connection | None = None
        self._write_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Open the write connection and configure SQLite PRAGMAs."""
        self._write_conn = await aiosqlite.connect(str(self._db_path))
        self._write_conn.row_factory = sqlite3.Row
        for pragma in _WRITE_PRAGMAS:
            await self._write_conn.execute(pragma)
        await self._write_conn.commit()
        logger.info("SQLiteConnectionPool started at %s", self._db_path)

    async def shutdown(self) -> None:
        """Commit any pending work and close the write connection."""
        if self._write_conn is not None:
            try:
                await self._write_conn.commit()
                await self._write_conn.close()
            except Exception as exc:
                logger.error("Error closing write connection: %s", exc)
            finally:
                self._write_conn = None
        logger.info("SQLiteConnectionPool shut down.")

    # Lifecycle protocol aliases (used by LifecycleCoordinator)
    async def initialize(self) -> None:
        await self.startup()

    # ------------------------------------------------------------------
    # Context managers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """Acquire the single write connection.

        Commits on clean exit; rolls back on exception.  The asyncio.Lock
        guarantees at most one writer at a time — the root fix for WAL
        write contention.
        """
        if self._write_conn is None:
            raise RuntimeError("SQLiteConnectionPool has not been started — call startup() first.")
        async with self._write_lock:
            try:
                yield self._write_conn
                await self._write_conn.commit()
            except Exception:
                try:
                    await self._write_conn.rollback()
                except Exception:
                    pass
                raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open a short-lived read connection and close it on exit.

        WAL mode allows any number of concurrent readers alongside the
        single writer, so there is no lock here.
        """
        conn = await aiosqlite.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            await conn.close()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        """``True`` if a write connection is open and the pool is ready."""
        return self._write_conn is not None

    def health_check(self) -> dict:
        """Return a health detail dict suitable for SubsystemHealthMonitor hooks."""
        return {
            "healthy": self.is_healthy,
            "db_path": str(self._db_path),
            "write_conn_open": self._write_conn is not None,
            "write_lock_locked": self._write_lock.locked(),
        }
