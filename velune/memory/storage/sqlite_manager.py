"""Thread-safe SQLite manager with WAL mode and connection pooling."""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.memory.storage.sqlite_manager")


class SQLiteManager:
    """Thread-safe SQLite manager with WAL mode and connection pooling.

    All memory tiers should use this manager instead of direct sqlite3.connect() calls.
    WAL (Write-Ahead Logging) mode allows concurrent readers with one writer.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_queue: queue.Queue = queue.Queue()
        self._is_running = True

        # Write thread health tracking and lock
        self._thread_lock = threading.Lock()
        self._thread_healthy = threading.Event()

        # Read connection pool
        self._local = threading.local()

        # Start the writer thread
        self._write_thread = threading.Thread(target=self._process_writes, daemon=True)
        self._write_thread.start()
        self._initialize_wal()

    def _initialize_wal(self) -> None:
        """Enable WAL mode for better concurrent read performance."""
        with self._read_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")

    @contextmanager
    def _read_connection(self):
        """Get a read connection. Multiple read connections are safe with WAL mode."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _get_read_connection(self):
        """Get or create a thread-local read connection.

        If the stored connection was closed externally (e.g. by :meth:`close`
        on a previous call in the same thread), a ``sqlite3.ProgrammingError``
        is raised on the next use.  We catch that case here and transparently
        recreate the connection so callers never see the error.
        """
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._new_read_connection()
        else:
            # Probe for a closed/stale handle without querying the DB
            try:
                self._local.conn.execute("SELECT 1")
            except Exception:
                # Connection was closed or is otherwise broken — recreate it
                try:
                    self._local.conn.close()
                except Exception:
                    pass
                self._local.conn = self._new_read_connection()
        return self._local.conn

    def _new_read_connection(self) -> sqlite3.Connection:
        """Create and configure a new read-mode SQLite connection."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _restart_write_thread(self) -> None:
        """Create a new write thread if the current one is dead."""
        with self._thread_lock:
            if not self._is_running:
                return
            if self._write_thread is None or not self._write_thread.is_alive():
                logger.info("SQLiteManager restarting dead write thread.")
                self._write_thread = threading.Thread(target=self._process_writes, daemon=True)
                self._write_thread.start()

    def _safe_put(self, item: tuple) -> None:
        """Restart the write thread if necessary, log queue warning if saturated, and queue write."""
        self._restart_write_thread()
        qsize = self._write_queue.qsize()
        if qsize > 50:
            logger.warning(
                "SQLiteManager write queue depth exceeds 50 (current depth: %d). "
                "Possible performance bottleneck or write queue saturation.",
                qsize,
            )
        self._write_queue.put(item)

    def execute_write(self, query: str, params: tuple = ()) -> None:
        """Queue a write operation. Fire and forget."""
        self._safe_put((query, params, None))

    def execute_write_sync(self, query: str, params: tuple = (), timeout: float = 15.0) -> None:
        """Queue a write operation and wait for completion.

        Raises:
            TimeoutError: If write doesn't complete within timeout.
                This indicates write thread death or severe queue backup.
            RuntimeError: If the write fails with a database error.
        """
        done = threading.Event()
        error_holder: list[Exception] = []
        self._safe_put((query, params, done, error_holder))

        # Log warning if taking longer than expected (5s)
        completed = done.wait(timeout=min(5.0, timeout))
        if not completed:
            if timeout > 5.0:
                logger.warning(
                    "SQLite write sync taking longer than expected (5s elapsed). Queue depth: %d",
                    self._write_queue.qsize(),
                )
                completed = done.wait(timeout=timeout - 5.0)

        if not completed:
            alive = self._write_thread.is_alive() if self._write_thread else False
            logger.critical(
                "SQLite write sync TIMEOUT exceeded (%ds). "
                "Queue depth: %d. "
                "Write thread alive: %s. "
                "Query: %s",
                timeout,
                self._write_queue.qsize(),
                alive,
                query[:100],
            )
            raise TimeoutError(
                f"SQLite write queue timeout after {timeout}s. "
                f"Queue depth: {self._write_queue.qsize()}. "
                f"Write thread alive: {alive}. "
                f"Query: {query[:100]}"
            )
        if error_holder:
            raise error_holder[0]

    def execute_write_many(self, queries: list[tuple[str, tuple]], timeout: float = 15.0) -> None:
        """Batch multiple writes as a single atomic transaction.

        Raises:
            TimeoutError: If batch write doesn't complete within timeout.
            RuntimeError: If the batch write fails.
        """
        done = threading.Event()
        error_holder: list[Exception] = []
        self._safe_put(("__BATCH__", queries, done, error_holder))

        # Log warning if taking longer than expected (5s)
        completed = done.wait(timeout=min(5.0, timeout))
        if not completed:
            if timeout > 5.0:
                logger.warning(
                    "SQLite batch write taking longer than expected (5s elapsed). Queue depth: %d",
                    self._write_queue.qsize(),
                )
                completed = done.wait(timeout=timeout - 5.0)

        if not completed:
            alive = self._write_thread.is_alive() if self._write_thread else False
            logger.critical(
                "SQLite batch write TIMEOUT exceeded (%ds). "
                "Batch size: %d. "
                "Queue depth: %d. "
                "Write thread alive: %s.",
                timeout,
                len(queries),
                self._write_queue.qsize(),
                alive,
            )
            raise TimeoutError(
                f"SQLite batch write timeout after {timeout}s. "
                f"Batch size: {len(queries)}. "
                f"Queue depth: {self._write_queue.qsize()}."
            )
        if error_holder:
            raise error_holder[0]

    def execute_read(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a read query. Thread-safe with WAL mode."""
        conn = self._get_read_connection()
        cursor = conn.execute(query, params)
        return cursor.fetchall()

    def execute_script(self, script: str, timeout: float = 10.0) -> None:
        """Execute a DDL script (CREATE TABLE, CREATE INDEX, etc.).

        Raises:
            TimeoutError: If script doesn't complete within timeout.
            RuntimeError: If the script fails.
        """
        done = threading.Event()
        error_holder: list[Exception] = []
        self._safe_put((script, None, done, error_holder))

        completed = done.wait(timeout=min(5.0, timeout))
        if not completed:
            if timeout > 5.0:
                logger.warning(
                    "SQLite script taking longer than expected (5s elapsed). Queue depth: %d",
                    self._write_queue.qsize(),
                )
                completed = done.wait(timeout=timeout - 5.0)

        if not completed:
            raise TimeoutError(f"SQLite script timeout after {timeout}s.")
        if error_holder:
            raise error_holder[0]

    def close(self) -> None:
        """Synchronously close the calling thread's read connection.

        Call this from the **same thread** that used :meth:`execute_read` to
        ensure the handle is released promptly.  On Windows this is critical:
        a lingering open handle prevents ``os.remove()`` from succeeding and
        causes ``PermissionError`` in tests.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            finally:
                self._local.conn = None

    def is_healthy(self) -> bool:
        """Returns True if the write thread is alive and queue depth is under 100."""
        return (
            self._write_thread is not None
            and self._write_thread.is_alive()
            and self._write_queue.qsize() < 100
        )

    def _process_writes(self) -> None:
        self._thread_healthy.set()
        try:
            while self._is_running:
                try:
                    item = self._write_queue.get(timeout=0.5)
                    if item is None:
                        self._write_queue.task_done()
                        break

                    # Support both 3-tuple (fire-and-forget) and 4-tuple (with error capture)
                    if len(item) == 4:
                        query, params, done_event, error_holder = item
                    else:
                        query, params, done_event = item
                        error_holder = None

                    try:
                        self._do_write(query, params)
                    except Exception as write_error:
                        logger.error(
                            "SQLite write error (query: %s): %s", str(query)[:80], write_error
                        )
                        if error_holder is not None:
                            error_holder.append(RuntimeError(f"SQLite write failed: {write_error}"))
                    finally:
                        if done_event:
                            done_event.set()

                    self._write_queue.task_done()

                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error("SQLite write thread loop error: %s", e)
        except Exception as e:
            logger.error("SQLite write thread unhandled exception: %s", e)
        finally:
            self._thread_healthy.clear()

    def _do_write(self, query: str, params: Any | None) -> None:
        last_error: Exception | None = None
        backoffs = [0.05, 0.1, 0.2, 0.4, 0.8]
        for attempt in range(5):
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=30.0)
                conn.execute("PRAGMA busy_timeout=5000")
                if query == "__BATCH__":
                    # params is actually a list of (query, params) tuples
                    for q, p in params:
                        conn.execute(q, p)
                elif params is None:
                    conn.executescript(query)
                else:
                    conn.execute(query, params)
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                last_error = e
                if "locked" in str(e) and attempt < 4:
                    backoff = backoffs[attempt]
                    logger.debug(
                        "SQLite lock contention in _do_write. "
                        "Attempt %d/5 failed with error: %s. Retrying in %ss...",
                        attempt + 1,
                        e,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                logger.error("Write failed after %d attempts: %s", attempt + 1, e)
                break
        if last_error is not None:
            raise last_error

    async def initialize(self) -> None:
        """Lifecycle start, no-op since thread starts in __init__."""
        pass

    async def shutdown(self) -> None:
        """Gracefully shut down the write processing thread.

        This is non-blocking with a timeout to prevent deadlocks if the write thread is dead.
        """
        self._is_running = False
        # Drain with timeout — do not block forever
        try:

            def drain():
                try:
                    self._write_queue.join()
                except Exception:
                    pass

            if self._write_thread.is_alive():
                t = threading.Thread(target=drain, daemon=True)
                t.start()
                t.join(timeout=2.0)
        except Exception:
            pass
        # Signal thread to exit
        self._write_queue.put(None)  # sentinel
        self._write_thread.join(timeout=5.0)

        # Clean up thread local connections
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
