"""Reciprocal Rank Fusion (RRF)."""

from typing import list, Dict


class ReciprocalRankFusion:
    """Reciprocal Rank Fusion for combining retrieval results."""

    def __init__(self, k: int = 60):
        self.k = k

    def fuse(
        self,
        result_lists: list[list[Dict[str, any]]],
        limit: int = 10,
    ) -> list[Dict[str, any]]:
        """Fuse multiple result lists using RRF."""
        scores: Dict[str, float] = {}
        all_results: Dict[str, Dict[str, any]] = {}
        
        for results in result_lists:
            for rank, result in enumerate(results):
                doc_id = result["id"]
                if doc_id not in all_results:
                    all_results[doc_id] = result
                
                # RRF score
                scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (self.k + rank + 1)
        
        # Sort by fused score
        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        
        # Return top results
        fused = []
        for doc_id, score in sorted_results[:limit]:
            result = all_results[doc_id].copy()
            result["fused_score"] = score
            fused.append(result)
        
        return fused
