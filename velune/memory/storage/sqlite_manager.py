from pathlib import Path
import sqlite3
import threading
import queue
import logging
import time
from typing import Any, Tuple, List, Optional
from contextlib import contextmanager

logger = logging.getLogger("velune.memory.storage.sqlite_manager")

class SQLiteManager:
    """Thread-safe SQLite manager with WAL mode and connection pooling.
    
    All memory tiers should use this manager instead of direct sqlite3.connect() calls.
    WAL (Write-Ahead Logging) mode allows concurrent readers with one writer.
    """
    
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_queue: queue.Queue[Tuple[str, tuple, Optional[threading.Event]]] = queue.Queue()
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
        """Queue a write operation and wait for completion."""
        done = threading.Event()
        self._write_queue.put((query, params, done))
        done.wait(timeout=10.0)
    
    def execute_read(self, query: str, params: tuple = ()) -> List[sqlite3.Row]:
        """Execute a read query. Thread-safe with WAL mode."""
        with self._read_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()
    
    def execute_script(self, script: str) -> None:
        """Execute a DDL script (CREATE TABLE, CREATE INDEX, etc.)."""
        done = threading.Event()
        self._write_queue.put((script, None, done))  # None params = script mode
        done.wait(timeout=10.0)
    
    def _process_writes(self) -> None:
        while self._is_running:
            try:
                item = self._write_queue.get(timeout=0.5)
                query, params, done_event = item
                self._do_write(query, params)
                if done_event:
                    done_event.set()
                self._write_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error("SQLite write error: %s", e)
    
    def _do_write(self, query: str, params: Optional[tuple]) -> None:
        for attempt in range(3):
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=30.0)
                conn.execute("PRAGMA busy_timeout=5000")
                if params is None:
                    conn.executescript(query)
                else:
                    conn.execute(query, params)
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                logger.error("Write failed after %d attempts: %s", attempt + 1, e)
                break
    
    async def initialize(self) -> None:
        """Lifecycle start, no-op since thread starts in __init__."""
        pass

    async def shutdown(self) -> None:
        """Gracefully shut down the write processing thread."""
        self._is_running = False
        self._write_queue.join()
        self._write_thread.join(timeout=2.0)
