"""Async single-writer SQLite connection pool.

Eliminates WAL write contention by serialising all writes through one
persistent connection guarded by an ``asyncio.Lock``.  Reads open a
short-lived second connection per call; WAL mode lets concurrent readers
coexist with the single writer without blocking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
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


def _is_corruption(exc: Exception) -> bool:
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text or "corrupt" in text


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
        """Open the write connection and configure SQLite PRAGMAs.

        If the database file is damaged ("database disk image is malformed" /
        "file is not a database" — e.g. after a killed process or a bad sync),
        it is moved aside as ``<name>.corrupt-<timestamp>`` and a fresh database
        is created, instead of leaving every memory subsystem degraded on every
        launch. The damaged file is kept so nothing is destroyed.
        """
        try:
            await self._open_and_verify()
        except sqlite3.DatabaseError as exc:
            if not _is_corruption(exc):
                raise
            await self._close_quietly()
            moved = self._quarantine_database(exc)
            await self._open_and_verify()
            logger.warning(
                "Memory database %s was damaged (%s); started a fresh one. "
                "The damaged copy was kept at %s.",
                self._db_path,
                exc,
                moved,
            )
        logger.info("SQLiteConnectionPool started at %s", self._db_path)

    async def _open_and_verify(self) -> None:
        self._write_conn = await aiosqlite.connect(str(self._db_path))
        self._write_conn.row_factory = sqlite3.Row
        for pragma in _WRITE_PRAGMAS:
            await self._write_conn.execute(pragma)
        # Cheap structural scan: touches every page, so damage that would
        # otherwise surface later as an error deep inside a query shows up here.
        cursor = await self._write_conn.execute("PRAGMA quick_check(1)")
        row = await cursor.fetchone()
        if row is not None and str(row[0]).lower() != "ok":
            raise sqlite3.DatabaseError(f"database disk image is malformed ({row[0]})")
        await self._write_conn.commit()

    async def _close_quietly(self) -> None:
        if self._write_conn is not None:
            try:
                await self._write_conn.close()
            except Exception:
                pass
            finally:
                self._write_conn = None

    def _quarantine_database(self, exc: Exception) -> Path:
        """Move the database and its -wal/-shm sidecars out of the way."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        moved = self._db_path.with_name(f"{self._db_path.name}.corrupt-{stamp}")
        for suffix in ("", "-wal", "-shm"):
            src = self._db_path.with_name(self._db_path.name + suffix)
            if src.exists():
                dst = moved if not suffix else moved.with_name(moved.name + suffix)
                try:
                    os.replace(src, dst)
                except OSError as move_exc:
                    logger.error("Could not move damaged file %s aside: %s", src, move_exc)
                    raise exc from move_exc
        return moved

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
