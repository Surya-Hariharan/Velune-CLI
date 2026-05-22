"""Hybrid retrieval orchestration."""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from velune.retrieval.lexical.memory import InMemoryLexicalIndex
from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalQuery, RetrievalResult, RetrievalSource
from velune.retrieval.vector.memory import InMemoryVectorStore


class HybridRetrievalEngine:
    """Combines lexical, vector, and future graph retrieval signals."""

    def __init__(self, vector_store: Optional[InMemoryVectorStore] = None, lexical_index: Optional[InMemoryLexicalIndex] = None) -> None:
        self.vector_store = vector_store or InMemoryVectorStore()
        self.lexical_index = lexical_index or InMemoryLexicalIndex()

    def index(self, documents: list[RetrievalDocument]) -> None:
        self.vector_store.upsert(documents)
        self.lexical_index.upsert(documents)

    def search(self, query: RetrievalQuery, embedding: Optional[list[float]] = None) -> RetrievalResult:
        lexical_hits = self.lexical_index.query(query.text, top_k=query.top_k, namespace=query.namespace, filters=query.filters)
        vector_hits = self.vector_store.query(embedding or [], top_k=query.top_k, namespace=query.namespace, filters=query.filters) if embedding else []

        fused = self._fuse(lexical_hits, vector_hits, query)
        return RetrievalResult(query=query, hits=fused, strategy="hybrid", metadata={"lexical_hits": len(lexical_hits), "vector_hits": len(vector_hits)})

    def _fuse(
        self,
        lexical_hits: list[tuple[RetrievalDocument, float]],
        vector_hits: list[tuple[RetrievalDocument, float]],
        query: RetrievalQuery,
    ) -> list[RetrievalHit]:
        scores: dict[str, float] = defaultdict(float)
        documents: dict[str, RetrievalDocument] = {}

        for rank, (document, score) in enumerate(lexical_hits, start=1):
            documents[document.id] = document
            scores[document.id] += query.lexical_weight * (1.0 / (60 + rank)) + score

        for rank, (document, score) in enumerate(vector_hits, start=1):
            documents[document.id] = document
            scores[document.id] += query.vector_weight * (1.0 / (60 + rank)) + score

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[: query.top_k]
        hits: list[RetrievalHit] = []
        vector_ids = {document.id for document, _ in vector_hits}
        for rank, (document_id, score) in enumerate(ranked, start=1):
            source = RetrievalSource.VECTOR if document_id in vector_ids else RetrievalSource.LEXICAL
            hits.append(RetrievalHit(document=documents[document_id], score=score, source=source, rank=rank))
        return hits