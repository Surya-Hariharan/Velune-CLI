"""BM25 Lexical retrieval layer for exact keyword matches."""

import re

from rank_bm25 import BM25Okapi

from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalSource


class BM25Retriever:
    """Retrieves context using BM25 exact keyword lexical indexing."""

    def __init__(self) -> None:
        self.documents: list[RetrievalDocument] = []
        self.corpus: list[list[str]] = []
        self.bm25: BM25Okapi | None = None

    def add_documents(self, docs: list[RetrievalDocument]) -> None:
        """Appends new documents to the lexical index corpus and rebuilds the BM25 model."""
        self.documents.extend(docs)
        self.corpus = [self._tokenize(doc.content) for doc in self.documents]
        if self.corpus:
            self.bm25 = BM25Okapi(self.corpus)

    def retrieve(self, query: str, top_k: int = 10, namespace: str | None = None) -> list[RetrievalHit]:
        """Queries the BM25 lexical index and scores candidates."""
        if not self.bm25 or not self.documents:
            return []

        tokens = self._tokenize(query)
        scores = self.bm25.get_scores(tokens)

        # Pair scores with documents and index ranks
        hits: list[RetrievalHit] = []
        for i, score in enumerate(scores):
            doc = self.documents[i]

            # Match namespace filter if provided
            if namespace and doc.namespace != namespace:
                continue

            if score > 0.0:  # Only capture positive keyword matches
                hits.append(
                    RetrievalHit(
                        document=doc,
                        score=float(score),
                        source=RetrievalSource.LEXICAL,
                        rank=0
                    )
                )

        # Sort and return top candidates
        hits.sort(key=lambda x: x.score, reverse=True)

        # Trim list and apply proper sequential ranks
        final_hits = hits[:top_k]
        for idx, h in enumerate(final_hits):
            h.rank = idx + 1

        return final_hits

    def _tokenize(self, text: str) -> list[str]:
        """Simplistic and quick alphanumeric tokenization ignoring standard case mappings."""
        # Lowercase and split on non-alphanumeric boundaries
        words = re.findall(r"\w+", text.lower())

        # Remove extremely common short English stop words to filter noise
        stop_words = {
            "a", "an", "the", "and", "or", "but", "if", "then", "else",
            "to", "of", "in", "for", "on", "with", "at", "by", "from", "is", "this", "that"
        }
        return [w for w in words if w not in stop_words and len(w) > 1]
