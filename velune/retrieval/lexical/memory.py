"""In-memory lexical index for local-first retrieval."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from math import log
from typing import Any, Optional

from velune.retrieval.schemas import RetrievalDocument


class InMemoryLexicalIndex:
    """A lightweight BM25-style lexical index."""

    def __init__(self) -> None:
        self._documents: dict[str, RetrievalDocument] = {}
        self._term_frequencies: dict[str, Counter[str]] = {}
        self._document_frequency: Counter[str] = Counter()

    def upsert(self, documents: list[RetrievalDocument]) -> None:
        for document in documents:
            tokens = self._tokenize(document.content)
            self._documents[document.id] = document
            self._term_frequencies[document.id] = Counter(tokens)
            for token in set(tokens):
                self._document_frequency[token] += 1

    def query(self, text: str, top_k: int = 10, namespace: Optional[str] = None, filters: Optional[dict[str, Any]] = None) -> list[tuple[RetrievalDocument, float]]:
        filters = filters or {}
        query_tokens = self._tokenize(text)
        if not query_tokens:
            return []

        results: list[tuple[RetrievalDocument, float]] = []
        total_documents = max(len(self._documents), 1)

        for document_id, document in self._documents.items():
            if namespace and document.namespace != namespace:
                continue
            if any(document.metadata.get(key) != value for key, value in filters.items()):
                continue

            term_frequencies = self._term_frequencies.get(document_id, Counter())
            score = 0.0
            for token in query_tokens:
                tf = term_frequencies.get(token, 0)
                if tf == 0:
                    continue
                idf = log((total_documents + 1) / (self._document_frequency.get(token, 0) + 1)) + 1.0
                score += tf * idf

            if score > 0:
                results.append((document, score))

        results.sort(key=lambda item: item[1], reverse=True)
        return results[:top_k]

    def _tokenize(self, text: str) -> list[str]:
        return [token.lower() for token in re.findall(r"[A-Za-z0-9_]+", text)]