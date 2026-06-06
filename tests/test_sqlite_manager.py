# tests/test_sqlite_manager.py

import threading
import time
import pytest
from pathlib import Path
from velune.memory.storage.sqlite_manager import SQLiteManager

def test_concurrent_writes_no_deadlock(tmp_path):
    """10 threads writing concurrently must all complete."""
    db = SQLiteManager(tmp_path / "test.db")
    db.execute_script("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    
    errors = []
    def write_worker(i):
        try:
            db.execute_write("INSERT INTO t VALUES (?)", (str(i),))
        except Exception as e:
            errors.append(e)
    
    threads = [threading.Thread(target=write_worker, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)
    
    # Wait for background queue to drain
    db._write_queue.join()
    
    assert not errors, f"Write errors: {errors}"
    rows = db.execute_read("SELECT COUNT(*) as c FROM t")
    assert rows[0]["c"] == 10

def test_shutdown_non_blocking(tmp_path):
    """Shutdown must complete within 8 seconds."""
    import asyncio
    db = SQLiteManager(tmp_path / "test.db")
    db.execute_script("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    
    start = time.time()
    asyncio.run(db.shutdown())
    elapsed = time.time() - start
    assert elapsed < 8.0, f"Shutdown took {elapsed:.1f}s"

def test_write_thread_health_check(tmp_path):
    """is_healthy() returns True after init."""
    db = SQLiteManager(tmp_path / "test.db")
    assert db.is_healthy()

def test_execute_read_thread_local(tmp_path):
    """execute_read from multiple threads must not share connections."""
    db = SQLiteManager(tmp_path / "test.db")
    db.execute_script("CREATE TABLE IF NOT EXISTS t (v INTEGER)")
    db.execute_write_sync("INSERT INTO t VALUES (42)")
    
    results = []
    def read_worker():
        rows = db.execute_read("SELECT v FROM t")
        results.append(rows[0]["v"])
    
    threads = [threading.Thread(target=read_worker) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert all(r == 42 for r in results)

def test_benchmark_concurrent_write_throughput(tmp_path):
    """Write queue must sustain at least 20 ops/sec (CI-safe floor; real hardware hits 1000+)."""
    db = SQLiteManager(tmp_path / "bench.db")
    db.execute_script("CREATE TABLE IF NOT EXISTS t (v TEXT, ts REAL)")

    N = 500
    start = time.time()
    for i in range(N):
        db.execute_write("INSERT INTO t VALUES (?, ?)", (str(i), time.time()))

    # Wait for queue to drain
    db._write_queue.join()
    elapsed = time.time() - start
    tps = N / elapsed
    print(f"Write throughput: {tps:.0f} ops/sec")
    assert tps > 20, f"Write throughput too low: {tps:.0f} ops/sec"
