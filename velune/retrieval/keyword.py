"""BM25 Lexical retrieval layer for exact keyword matches."""

import logging
import re
import threading
import time

from rank_bm25 import BM25Okapi

from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalSource

logger = logging.getLogger("velune.retrieval.keyword")


class BM25Retriever:
    """Retrieves context using BM25 exact keyword lexical indexing."""

    def __init__(self) -> None:
        self.documents: list[RetrievalDocument] = []
        self.corpus: list[list[str]] = []
        self.bm25: BM25Okapi | None = None
        self._dirty: bool = False  # True when corpus needs rebuild
        self._rebuild_lock = threading.Lock()

    @property
    def index_size(self) -> int:
        return len(self.documents)

    def add_documents(self, docs: list[RetrievalDocument]) -> None:
        """Appends new documents to the lexical index corpus and marks the index as dirty."""
        for doc in docs:
            self.documents.append(doc)
            self.corpus.append(self._tokenize(doc.content))
        self._dirty = True
        self.bm25 = None  # Invalidate current index
        logger.debug("BM25 index marked dirty: %d total documents", len(self.documents))

    def add_documents_batch(self, docs: list[RetrievalDocument]) -> None:
        """More efficient batch add — tokenizes new documents and appends them to corpus."""
        new_tokens = [self._tokenize(doc.content) for doc in docs]
        self.documents.extend(docs)
        self.corpus.extend(new_tokens)
        self._dirty = True
        self.bm25 = None
        logger.debug("BM25 index marked dirty: %d total documents", len(self.documents))

    def _ensure_index(self) -> None:
        """Rebuild BM25 index if dirty. Thread-safe."""
        if not self._dirty or not self.corpus:
            return
        with self._rebuild_lock:
            if not self._dirty:  # Double-check after acquiring lock
                return
            start_time = time.time()
            self.bm25 = BM25Okapi(self.corpus)
            self._dirty = False
            elapsed = time.time() - start_time
            logger.debug("BM25 index rebuilt: %d documents, %.3fs", len(self.documents), elapsed)

    def retrieve(
        self, query: str, top_k: int = 10, namespace: str | None = None
    ) -> list[RetrievalHit]:
        """Queries the BM25 lexical index and scores candidates."""
        if not self.documents:
            return []

        if self._dirty:
            self._ensure_index()

        if not self.bm25:
            return []

        tokens = self._tokenize(query)
        scores = self.bm25.get_scores(tokens)

        # Candidate selection is by token *overlap*, not by a positive BM25
        # score. BM25Okapi's IDF — ln((N - df + 0.5) / (df + 0.5)) — is exactly
        # 0 when a term appears in half the corpus, and ≤ 0 for every term in
        # 1–2-document corpora, so a `score > 0` filter silently discards
        # verbatim matches in small workspaces (the common "first try in a
        # scratch repo" case) and lexical retrieval returns nothing at all.
        # Ranking still uses the BM25 score first (healthy corpora are
        # unaffected), with overlap as the tiebreak for the degenerate case.
        query_tokens = set(tokens)
        scored: list[tuple[float, int, RetrievalHit]] = []
        for i, score in enumerate(scores):
            doc = self.documents[i]

            # Match namespace filter if provided
            if namespace and doc.namespace != namespace:
                continue

            overlap = len(query_tokens.intersection(self.corpus[i]))
            if overlap == 0:
                continue
            scored.append(
                (
                    float(score),
                    overlap,
                    RetrievalHit(
                        document=doc,
                        # RetrievalHit scores must be non-negative; degenerate
                        # (zero/negative-IDF) matches carry 0.0 and rely on
                        # hybrid-fusion normalization downstream.
                        score=max(0.0, float(score)),
                        source=RetrievalSource.LEXICAL,
                        rank=0,
                    ),
                )
            )

        # Sort by BM25 score, then overlap; trim and apply sequential ranks.
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        final_hits = [h for _, _, h in scored[:top_k]]
        for idx, h in enumerate(final_hits):
            h.rank = idx + 1

        return final_hits

    def _tokenize(self, text: str) -> list[str]:
        """Simplistic and quick alphanumeric tokenization ignoring standard case mappings."""
        # Lowercase and split on non-alphanumeric boundaries
        words = re.findall(r"\w+", text.lower())

        # Remove extremely common short English stop words to filter noise
        stop_words = {
            "a",
            "an",
            "the",
            "and",
            "or",
            "but",
            "if",
            "then",
            "else",
            "to",
            "of",
            "in",
            "for",
            "on",
            "with",
            "at",
            "by",
            "from",
            "is",
            "this",
            "that",
        }
        return [w for w in words if w not in stop_words and len(w) > 1]
