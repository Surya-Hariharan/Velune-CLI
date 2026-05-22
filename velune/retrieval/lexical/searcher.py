"""BM25 searcher."""

from typing import list, Dict, Optional
from velune.retrieval.lexical.index import BM25Index


class BM25Searcher:
    """Searcher using BM25 lexical search."""

    def __init__(self, index: BM25Index):
        self.index = index

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, any]] = None,
    ) -> list[Dict[str, any]]:
        """Search using BM25."""
        results = self.index.search(query, k=limit)
        
        formatted_results = []
        for doc_id, score in results:
            result = {
                "id": doc_id,
                "score": score,
                "retrieval_method": "bm25",
            }
            if filters:
                # Apply filters if provided
                pass
            formatted_results.append(result)
        
        return formatted_results
