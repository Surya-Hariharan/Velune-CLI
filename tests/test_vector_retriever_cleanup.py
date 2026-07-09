"""Regression tests for VectorRetriever point-ID determinism and deletion.

Two bugs, only visible together: (1) upsert() derived Qdrant point IDs from
Python's built-in hash(), which is randomly salted per interpreter process
(PYTHONHASHSEED) — so the same document ID mapped to a *different* point on
every run, meaning re-indexing the same file across sessions could never
overwrite its own prior point (silent duplication), and (2) there was no
deletion method at all, so removed files' embeddings were never purged
(orphaned vectors, unbounded growth). Both matter together: even a delete
method would have been useless without a stable, re-derivable point ID.
"""

from __future__ import annotations

from velune.retrieval.schemas import RetrievalDocument
from velune.retrieval.vector import VectorRetriever, _point_id


def test_point_id_is_stable_across_separate_calls():
    """Simulates "two different process runs": _point_id must not depend on
    any per-process random state (unlike Python's hash())."""
    assert _point_id("src/main.py") == _point_id("src/main.py")
    assert _point_id("src/a.py") != _point_id("src/b.py")


def test_point_id_is_a_valid_qdrant_uint64():
    assert 0 <= _point_id("anything") < 2**63


def test_upsert_then_delete_round_trip():
    retriever = VectorRetriever(location=":memory:")
    doc = RetrievalDocument(
        id="src/main.py",
        content="def main(): pass",
        metadata={"path": "src/main.py"},
        embedding=[0.1] * 8,
    )
    retriever.upsert(doc)

    hits = retriever.retrieve([0.1] * 8, top_k=5)
    assert [h.document.id for h in hits] == ["src/main.py"]

    retriever.delete_by_ids(["src/main.py"])

    assert retriever.retrieve([0.1] * 8, top_k=5) == []


def test_reindexing_the_same_file_overwrites_rather_than_duplicates():
    """The actual production scenario: a file is re-parsed and re-embedded on
    every incremental index. Before the fix, a new random point ID could
    coexist with the old one instead of updating it."""
    retriever = VectorRetriever(location=":memory:")
    first = RetrievalDocument(id="src/main.py", content="v1", embedding=[0.1] * 8)
    second = RetrievalDocument(id="src/main.py", content="v2", embedding=[0.2] * 8)

    retriever.upsert(first)
    retriever.upsert(second)

    hits = retriever.retrieve([0.2] * 8, top_k=10)
    assert len(hits) == 1
    assert hits[0].document.content == "v2"


def test_delete_by_ids_is_a_no_op_when_nothing_was_ever_indexed():
    retriever = VectorRetriever(location=":memory:")
    retriever.delete_by_ids(["never/indexed.py"])  # must not raise


def test_delete_by_ids_with_empty_list_is_a_no_op():
    retriever = VectorRetriever(location=":memory:")
    retriever.delete_by_ids([])  # must not raise, must not touch the client

    # No client should even have been resolved for an empty delete.
    assert retriever._client_resolved is False
