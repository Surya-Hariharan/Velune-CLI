"""RepositoryIntelligenceEngine._handle_graph_patch must purge vector-store
entries for files removed since the last index, using the optional
``retrieval`` dependency (absent by default; the engine degrades gracefully
without it — see velune/intelligence/subsystems.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from velune.intelligence.engine import RepositoryIntelligenceEngine


def _make_engine(*, retrieval=None) -> RepositoryIntelligenceEngine:
    engine = RepositoryIntelligenceEngine(
        workspace=Path(".").resolve(),
        cognition=MagicMock(),
        knowledge_graph=MagicMock(),
        bus=MagicMock(),
        retrieval=retrieval,
    )
    engine._patcher.patch = AsyncMock(
        return_value=MagicMock(nodes_added=0, nodes_removed=1, edges_added=0)
    )
    engine._emit = AsyncMock()
    return engine


def _delta(to_remove):
    d = MagicMock()
    d.to_remove = to_remove
    return d


async def test_removed_files_are_purged_from_the_vector_store():
    retrieval = MagicMock()
    retrieval.vector_retriever.delete_by_ids = MagicMock()
    engine = _make_engine(retrieval=retrieval)

    await engine._handle_graph_patch(_delta(["deleted/file.py"]))

    retrieval.vector_retriever.delete_by_ids.assert_called_once_with(["deleted/file.py"])


async def test_no_vector_cleanup_call_when_nothing_was_removed():
    retrieval = MagicMock()
    retrieval.vector_retriever.delete_by_ids = MagicMock()
    engine = _make_engine(retrieval=retrieval)

    await engine._handle_graph_patch(_delta([]))

    retrieval.vector_retriever.delete_by_ids.assert_not_called()


async def test_graph_patch_still_succeeds_without_a_retrieval_dependency():
    """retrieval=None is the normal, fully-supported state (see subsystems.py
    factory) — graph patching must not be skipped or broken by its absence."""
    engine = _make_engine(retrieval=None)

    await engine._handle_graph_patch(_delta(["deleted/file.py"]))  # must not raise

    engine._patcher.patch.assert_awaited_once()
    engine._emit.assert_awaited_once()


async def test_vector_cleanup_failure_does_not_block_the_graph_patch_event():
    retrieval = MagicMock()
    retrieval.vector_retriever.delete_by_ids = MagicMock(side_effect=RuntimeError("qdrant down"))
    engine = _make_engine(retrieval=retrieval)

    await engine._handle_graph_patch(_delta(["deleted/file.py"]))  # must not raise

    engine._emit.assert_awaited_once()  # the knowledge-graph-patched event still fired
