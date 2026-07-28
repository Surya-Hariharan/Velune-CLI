"""Regression tests for the index_state.json concurrent-writer race.

``RepositoryIntelligenceEngine``'s background loop, ``RepositoryCognitionService
.run_incremental()``, and a full ``RepositoryCognitionService.index()`` run
all read-modify-write the same ``.velune/index_state.json`` independently.
Before the fix in ``velune/repository/index_state.py`` (``index_state_lock``
/ ``index_state_lock_async``), two overlapping writers could each load a
stale snapshot and the one that saved last would silently discard the
other's changes wholesale — not a torn file, a genuine lost update.
"""

from __future__ import annotations

import asyncio
import threading

from velune.repository.incremental_indexer import IncrementalIndexer, IndexDelta
from velune.repository.index_state import (
    IndexedFile,
    IndexState,
    index_state_lock,
    index_state_lock_async,
)


def test_sync_lock_prevents_lost_updates(tmp_path):
    """N threads each do a non-atomic read-increment-write under the lock."""
    state_path = tmp_path / "index_state.json"
    IndexState.empty(str(tmp_path)).save(state_path)

    def bump():
        with index_state_lock(state_path):
            state = IndexState.load(state_path)
            current = int(state.file_index.get("counter", IndexedFile("counter", "", "", 0, 0.0)).symbol_count)
            # A deliberate gap between read and write — without the lock,
            # this is exactly where another thread's update gets lost.
            state.file_index["counter"] = IndexedFile(
                path="counter", content_hash="", language="", symbol_count=current + 1, indexed_at=0.0
            )
            state.save(state_path)

    threads = [threading.Thread(target=bump) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = IndexState.load(state_path)
    assert final.file_index["counter"].symbol_count == 20


async def test_async_lock_prevents_lost_updates(tmp_path):
    """N coroutines each do a non-atomic read-increment-write under the async lock."""
    state_path = tmp_path / "index_state.json"
    IndexState.empty(str(tmp_path)).save(state_path)

    async def bump():
        async with index_state_lock_async(state_path):
            state = IndexState.load(state_path)
            current = int(state.file_index.get("counter", IndexedFile("counter", "", "", 0, 0.0)).symbol_count)
            await asyncio.sleep(0)  # yield control mid-critical-section on purpose
            state.file_index["counter"] = IndexedFile(
                path="counter", content_hash="", language="", symbol_count=current + 1, indexed_at=0.0
            )
            state.save(state_path)

    await asyncio.gather(*(bump() for _ in range(20)))

    final = IndexState.load(state_path)
    assert final.file_index["counter"].symbol_count == 20


async def test_concurrent_apply_delta_calls_both_survive(tmp_path):
    """Two concurrent IncrementalIndexer.apply_delta calls on disjoint files
    must both be reflected in the final state — neither should clobber the
    other's file_index entry."""
    root = tmp_path
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    state_path = root / ".velune" / "index_state.json"

    inc_a = IncrementalIndexer(root, state_path)
    inc_b = IncrementalIndexer(root, state_path)

    delta_a = IndexDelta(to_add=["a.py"])
    delta_b = IndexDelta(to_add=["b.py"])

    await asyncio.gather(inc_a.apply_delta(delta_a), inc_b.apply_delta(delta_b))

    final = IndexState.load(state_path)
    assert "a.py" in final.file_index, "concurrent writer's update was lost"
    assert "b.py" in final.file_index, "concurrent writer's update was lost"
