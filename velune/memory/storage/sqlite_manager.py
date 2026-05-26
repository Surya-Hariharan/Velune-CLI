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

    def execute_write(self, query: str, params: tuple = ()) -> None:
        """Queue a write operation. Fire and forget."""
        self._write_queue.put((query, params, None))

    def execute_write_sync(self, query: str, params: tuple = ()) -> None:
        """Queue a write operation and wait for completion.
        
        Raises:
            TimeoutError: If write doesn't complete within 10 seconds.
                This indicates write thread death or severe queue backup.
            RuntimeError: If the write fails with a database error.
        """
        done = threading.Event()
        error_holder: list[Exception] = []
        self._write_queue.put((query, params, done, error_holder))
        
        completed = done.wait(timeout=10.0)
        if not completed:
            raise TimeoutError(
                f"SQLite write queue timeout after 10s. "
                f"Queue depth: {self._write_queue.qsize()}. "
                f"Write thread alive: {self._write_thread.is_alive()}. "
                f"Query: {query[:100]}"
            )
        if error_holder:
            raise error_holder[0]

    def execute_write_many(self, queries: list[tuple[str, tuple]]) -> None:
        """Batch multiple writes as a single atomic transaction.
        
        Raises:
            TimeoutError: If batch write doesn't complete within 15 seconds.
            RuntimeError: If the batch write fails.
        """
        done = threading.Event()
        error_holder: list[Exception] = []
        self._write_queue.put(("__BATCH__", queries, done, error_holder))
        
        completed = done.wait(timeout=15.0)
        if not completed:
            raise TimeoutError(
                f"SQLite batch write timeout after 15s. "
                f"Batch size: {len(queries)}. "
                f"Queue depth: {self._write_queue.qsize()}."
            )
        if error_holder:
            raise error_holder[0]

    def execute_read(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a read query. Thread-safe with WAL mode."""
        with self._read_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def execute_script(self, script: str) -> None:
        """Execute a DDL script (CREATE TABLE, CREATE INDEX, etc.).
        
        Raises:
            TimeoutError: If script doesn't complete within 10 seconds.
            RuntimeError: If the script fails.
        """
        done = threading.Event()
        error_holder: list[Exception] = []
        self._write_queue.put((script, None, done, error_holder))
        
        completed = done.wait(timeout=10.0)
        if not completed:
            raise TimeoutError(f"SQLite script timeout after 10s.")
        if error_holder:
            raise error_holder[0]

    def _process_writes(self) -> None:
        while self._is_running:
            try:
                item = self._write_queue.get(timeout=0.5)
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
                        "SQLite write error (query: %s): %s",
                        str(query)[:80],
                        write_error
                    )
                    if error_holder is not None:
                        error_holder.append(
                            RuntimeError(f"SQLite write failed: {write_error}")
                        )
                finally:
                    if done_event:
                        done_event.set()
                
                self._write_queue.task_done()
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.error("SQLite write thread error: %s", e)

    def _do_write(self, query: str, params: Any | None) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
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
                if "locked" in str(e) and attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
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
        
        This is a no-op except for waiting on current writes to complete (flushing),
        to ensure the thread remains running across CLI commands.
        """
        self._write_queue.join()
