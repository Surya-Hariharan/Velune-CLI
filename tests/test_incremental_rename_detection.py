"""Regression tests for rename detection in the incremental indexer.

``docs/REPOSITORY_INTELLIGENCE_BASELINE.md`` §3.3/§4.2: "Renames are not
detected as renames — they compute as a to_remove + to_add pair, meaning
anything keyed by the old path (symbol IDs, staleness trackers) goes stale
until the next successful patch cycle recreates it under the new path."

``IncrementalIndexer._detect_renames`` correlates a to_remove path's stored
content hash against to_add paths' computed hashes; ``apply_delta`` then
uses ``_reindex_renamed`` to carry the old entry's language/symbol_count
forward instead of re-reading and re-parsing content that's provably
unchanged — the whole rename lands in a single delta/apply_delta call
rather than needing two.
"""

from __future__ import annotations

import asyncio

from velune.repository.incremental_indexer import IncrementalIndexer, IndexDelta
from velune.repository.index_state import IndexState


def _run(coro):
    return asyncio.run(coro)


def test_pure_rename_is_detected_and_reuses_old_symbol_data(tmp_path):
    state_path = tmp_path / ".velune" / "index_state.json"
    (tmp_path / "old_name.py").write_text(
        "def helper():\n    return 1\n\n\nclass Widget:\n    pass\n", encoding="utf-8"
    )

    inc = IncrementalIndexer(tmp_path, state_path)
    _run(inc.apply_delta(_run(inc.compute_delta())))
    original_symbol_count = IndexState.load(state_path).file_index["old_name.py"].symbol_count
    assert original_symbol_count > 0

    (tmp_path / "old_name.py").rename(tmp_path / "new_name.py")

    inc2 = IncrementalIndexer(tmp_path, state_path)
    delta = _run(inc2.compute_delta())
    assert delta.renames == [("old_name.py", "new_name.py")]
    assert "new_name.py" in delta.to_add
    assert "old_name.py" in delta.to_remove

    _run(inc2.apply_delta(delta))
    final_state = IndexState.load(state_path)
    assert "old_name.py" not in final_state.file_index
    assert "new_name.py" in final_state.file_index
    assert final_state.file_index["new_name.py"].symbol_count == original_symbol_count


def test_rename_plus_content_edit_is_not_treated_as_a_pure_rename(tmp_path):
    """A rename where the content also changed has a different hash — this
    must fall through to an ordinary delete + add + re-parse, not a false
    rename match with stale symbol data."""
    state_path = tmp_path / ".velune" / "index_state.json"
    (tmp_path / "old_name.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    inc = IncrementalIndexer(tmp_path, state_path)
    _run(inc.apply_delta(_run(inc.compute_delta())))

    (tmp_path / "old_name.py").unlink()
    (tmp_path / "new_name.py").write_text(
        "def helper():\n    return 1\n\n\ndef extra():\n    return 2\n", encoding="utf-8"
    )

    inc2 = IncrementalIndexer(tmp_path, state_path)
    delta = _run(inc2.compute_delta())
    assert delta.renames == []
    assert "new_name.py" in delta.to_add
    assert "old_name.py" in delta.to_remove

    _run(inc2.apply_delta(delta))
    final_state = IndexState.load(state_path)
    # Re-parsed for real (2 functions), not a stale count copied from the
    # single-function old file.
    assert final_state.file_index["new_name.py"].symbol_count == 2


def test_unrelated_add_and_remove_with_different_content_is_not_a_rename(tmp_path):
    state_path = tmp_path / ".velune" / "index_state.json"
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")

    inc = IncrementalIndexer(tmp_path, state_path)
    _run(inc.apply_delta(_run(inc.compute_delta())))

    (tmp_path / "a.py").unlink()
    (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")

    inc2 = IncrementalIndexer(tmp_path, state_path)
    delta = _run(inc2.compute_delta())
    assert delta.renames == []


def test_ambiguous_duplicate_content_claims_each_candidate_at_most_once(tmp_path):
    """Two removed files with identical content and two added files with
    that same content must not double-claim a single add as the rename
    target for both removes."""
    state_path = tmp_path / ".velune" / "index_state.json"
    (tmp_path / "a.py").write_text("def same():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def same():\n    return 1\n", encoding="utf-8")

    inc = IncrementalIndexer(tmp_path, state_path)
    _run(inc.apply_delta(_run(inc.compute_delta())))

    (tmp_path / "a.py").unlink()
    (tmp_path / "b.py").unlink()
    (tmp_path / "c.py").write_text("def same():\n    return 1\n", encoding="utf-8")
    (tmp_path / "d.py").write_text("def same():\n    return 1\n", encoding="utf-8")

    inc2 = IncrementalIndexer(tmp_path, state_path)
    delta = _run(inc2.compute_delta())

    claimed_targets = [new for _, new in delta.renames]
    assert len(claimed_targets) == len(set(claimed_targets)), "each add claimed at most once"
    assert len(delta.renames) == 2
