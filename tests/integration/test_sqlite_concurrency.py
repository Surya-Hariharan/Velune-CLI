"""Integration tests for SQLiteConnectionPool write-contention elimination.

Verifies that 20 concurrent async write tasks complete without any
"database is locked" / OperationalError, that all writes are committed
and readable afterwards, and that the total wall-clock time is reasonable
(< 2 seconds for 20 simple inserts on local disk).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from velune.memory.storage.sqlite_pool import SQLiteConnectionPool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_pool(db_path: Path) -> SQLiteConnectionPool:
    pool = SQLiteConnectionPool(db_path)
    await pool.startup()
    async with pool.write() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS writes (id INTEGER PRIMARY KEY, val TEXT NOT NULL)"
        )
    return pool


async def _write_one(pool: SQLiteConnectionPool, row_id: int) -> None:
    """Insert a single row — will raise on any SQLite error."""
    async with pool.write() as conn:
        await conn.execute(
            "INSERT INTO writes (id, val) VALUES (?, ?)",
            (row_id, f"task-{row_id}"),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_20_concurrent_writes_no_contention(tmp_path: Path) -> None:
    """20 concurrent writes must all succeed with zero OperationalError exceptions."""
    db = tmp_path / "concurrent.db"
    pool = await _make_pool(db)

    errors: list[Exception] = []

    async def safe_write(i: int) -> None:
        try:
            await _write_one(pool, i)
        except Exception as exc:
            errors.append(exc)

    start = time.perf_counter()
    await asyncio.gather(*[safe_write(i) for i in range(20)])
    elapsed = time.perf_counter() - start

    await pool.shutdown()

    assert errors == [], (
        f"Expected zero write errors; got {len(errors)}: "
        + "; ".join(str(e) for e in errors[:3])
    )
    assert elapsed < 2.0, (
        f"20 concurrent writes took {elapsed:.3f}s — expected < 2s. "
        "Pool serialisation overhead is too high."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_20_writes_committed_and_readable(tmp_path: Path) -> None:
    """All 20 rows must be readable after concurrent writes complete."""
    db = tmp_path / "readable.db"
    pool = await _make_pool(db)

    await asyncio.gather(*[_write_one(pool, i) for i in range(20)])

    async with pool.read() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM writes")
        row = await cursor.fetchone()
        count = row[0]

    await pool.shutdown()

    assert count == 20, (
        f"Expected 20 committed rows after concurrent writes; found {count}. "
        "Some writes were lost or not committed."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_locked_errors_in_gathered_writes(tmp_path: Path) -> None:
    """'database is locked' must never appear in any exception from gathered writes."""
    db = tmp_path / "locked_check.db"
    pool = await _make_pool(db)

    caught: list[str] = []

    async def guarded_write(i: int) -> None:
        try:
            await _write_one(pool, i)
        except Exception as exc:
            caught.append(str(exc))

    await asyncio.gather(*[guarded_write(i) for i in range(20)])
    await pool.shutdown()

    locked = [msg for msg in caught if "locked" in msg.lower() or "operationalerror" in msg.lower()]
    assert locked == [], (
        f"Got {len(locked)} 'database is locked' / OperationalError messages: {locked[:3]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_healthy_reflects_pool_state(tmp_path: Path) -> None:
    """is_healthy must be False before startup and True after, False again after shutdown."""
    pool = SQLiteConnectionPool(tmp_path / "health.db")

    assert pool.is_healthy is False, "Pool must be unhealthy before startup()"

    await pool.startup()
    assert pool.is_healthy is True, "Pool must be healthy after startup()"

    await pool.shutdown()
    assert pool.is_healthy is False, "Pool must be unhealthy after shutdown()"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_context_manager_rolls_back_on_error(tmp_path: Path) -> None:
    """A write that raises inside the context must be rolled back cleanly."""
    db = tmp_path / "rollback.db"
    pool = await _make_pool(db)

    with pytest.raises(ValueError, match="forced"):
        async with pool.write() as conn:
            await conn.execute("INSERT INTO writes (id, val) VALUES (99, 'x')")
            raise ValueError("forced rollback")

    # Row 99 must not have been committed.
    async with pool.read() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM writes WHERE id = 99")
        row = await cursor.fetchone()

    await pool.shutdown()
    assert row[0] == 0, "Row inserted before a raised exception must be rolled back"
