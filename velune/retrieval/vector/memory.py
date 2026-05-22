"""In-memory vector store used as the local-first default."""

from __future__ import annotations

import math
from typing import Any, Optional

from velune.retrieval.schemas import RetrievalDocument


class InMemoryVectorStore:
    """Simple cosine-similarity vector store.

    This keeps the abstraction replaceable with Chroma, Qdrant, Weaviate, or LanceDB.
    """

    def __init__(self) -> None:
        self._documents: dict[str, RetrievalDocument] = {}

    def upsert(self, documents: list[RetrievalDocument]) -> None:
        for document in documents:
            self._documents[document.id] = document

    def delete(self, ids: list[str]) -> None:
        for document_id in ids:
            self._documents.pop(document_id, None)

    def get(self, document_id: str) -> Optional[RetrievalDocument]:
        return self._documents.get(document_id)

    def query(
        self,
        embedding: list[float],
        top_k: int = 10,
        namespace: Optional[str] = None,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[tuple[RetrievalDocument, float]]:
        filters = filters or {}
        results: list[tuple[RetrievalDocument, float]] = []

        for document in self._documents.values():
            if namespace and document.namespace != namespace:
                continue
            if any(document.metadata.get(key) != value for key, value in filters.items()):
                continue
            if document.embedding is None:
                continue
            score = self._cosine_similarity(embedding, document.embedding)
            results.append((document, score))

        results.sort(key=lambda item: item[1], reverse=True)
        return results[:top_k]

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        numerator = sum(l * r for l, r in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return numerator / (left_norm * right_norm)