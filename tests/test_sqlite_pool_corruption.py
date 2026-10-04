"""A damaged memory database is quarantined and replaced, not left to degrade every subsystem."""

from __future__ import annotations

import sqlite3

import pytest

from velune.memory.storage.sqlite_pool import SQLiteConnectionPool


def _make_db(path, rows: int = 400) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO t (body) VALUES (?)", [("x" * 200,)] * rows)
    conn.commit()
    conn.close()


async def _roundtrip(pool: SQLiteConnectionPool) -> int:
    async with pool.write() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS fresh (v INTEGER)")
        await conn.execute("INSERT INTO fresh (v) VALUES (1)")
    async with pool.read() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM fresh")
        return (await cursor.fetchone())[0]


async def test_healthy_database_is_untouched(tmp_path):
    db = tmp_path / "core.db"
    _make_db(db)
    pool = SQLiteConnectionPool(db)
    await pool.startup()
    try:
        async with pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM t")
            assert (await cursor.fetchone())[0] == 400
    finally:
        await pool.shutdown()
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("damage", ["header", "middle_pages"])
async def test_damaged_database_is_quarantined_and_recreated(tmp_path, damage):
    db = tmp_path / "core.db"
    _make_db(db)
    data = bytearray(db.read_bytes())
    if damage == "header":
        data[:100] = b"\x00" * 100  # destroys the 'SQLite format 3' header
    else:
        data[4096 * 3 : 4096 * 6] = b"\xff" * (4096 * 3)  # garbage in interior pages
    db.write_bytes(bytes(data))

    pool = SQLiteConnectionPool(db)
    await pool.startup()
    try:
        assert await _roundtrip(pool) == 1  # a fresh, working database
    finally:
        await pool.shutdown()

    kept = list(tmp_path.glob("core.db.corrupt-*"))
    assert len(kept) == 1  # the damaged file is preserved, not deleted
    assert kept[0].stat().st_size == len(data)


async def test_unrelated_errors_are_not_treated_as_corruption(tmp_path):
    # A directory where the file should be is an I/O problem, not corruption:
    # it must surface instead of silently moving things around.
    db = tmp_path / "core.db"
    db.mkdir()
    pool = SQLiteConnectionPool(db)
    with pytest.raises(sqlite3.Error):
        await pool.startup()
    assert db.is_dir()
