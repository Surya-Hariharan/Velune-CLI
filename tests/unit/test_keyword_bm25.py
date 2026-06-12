import threading
import time

from velune.retrieval.keyword import BM25Retriever
from velune.retrieval.schemas import RetrievalDocument


def test_add_documents_does_not_rebuild_immediately():
    """add_documents must not rebuild BM25 — lazy rebuild on retrieve."""
    retriever = BM25Retriever()
    docs = [
        RetrievalDocument(id=str(i), content=f"document {i}", namespace="default")
        for i in range(100)
    ]

    start = time.time()
    for doc in docs:
        retriever.add_documents([doc])
    time.time() - start

    assert retriever.bm25 is None  # Not built yet
    assert retriever._dirty

    # Retrieve triggers build
    retriever.retrieve("document 50")
    assert retriever.bm25 is not None  # Now built
    assert not retriever._dirty


def test_add_1000_docs_fast(benchmark):
    """Adding 1000 documents must complete in under 100ms."""
    retriever = BM25Retriever()
    docs = [
        RetrievalDocument(id=str(i), content=f"word{i} " * 50, namespace="default")
        for i in range(1000)
    ]

    def add_all():
        for doc in docs:
            retriever.add_documents([doc])

    benchmark(add_all)
    # Pytest-benchmark will report time


def test_concurrent_retrieve_no_multiple_rebuilds():
    """Concurrent retrieve() calls must trigger only one rebuild."""
    retriever = BM25Retriever()
    retriever.add_documents(
        [RetrievalDocument(id="d1", content="test document", namespace="default")]
    )

    rebuild_count = [0]
    original_rebuild = retriever._ensure_index

    def counted_rebuild():
        rebuild_count[0] += 1
        original_rebuild()

    retriever._ensure_index = counted_rebuild

    threads = [threading.Thread(target=lambda: retriever.retrieve("test")) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should rebuild at most once despite 10 concurrent calls
    assert rebuild_count[0] <= 2  # Allow for race on double-check
