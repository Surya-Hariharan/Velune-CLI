"""BM25 index."""

from typing import list, Dict
from rank_bm25 import BM25Okapi
import re


class BM25Index:
    """BM25 lexical search index."""

    def __init__(self):
        self.documents: list[str] = []
        self.tokenized_docs: list[list[str]] = []
        self.index: Optional[BM25Okapi] = None
        self.doc_ids: list[str] = []

    def add_document(self, doc_id: str, document: str) -> None:
        """Add a document to the index."""
        self.documents.append(document)
        self.doc_ids.append(doc_id)
        self.tokenized_docs.append(self._tokenize(document))

    def build_index(self) -> None:
        """Build the BM25 index."""
        self.index = BM25Okapi(self.tokenized_docs)

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        """Search the index."""
        if self.index is None:
            self.build_index()
        
        tokenized_query = self._tokenize(query)
        scores = self.index.get_scores(tokenized_query)
        
        # Get top k results
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        
        return [(self.doc_ids[i], scores[i]) for i in top_indices if scores[i] > 0]

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize text."""
        # Simple tokenization
        text = text.lower()
        text = re.sub(r'[^a-z0-9\s]', '', text)
        return text.split()
