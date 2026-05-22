"""Cross-encoder reranking."""

from typing import list, Dict


class CrossEncoderReranker:
    """Reranks results using cross-encoder."""

    def __init__(self):
        # In production, initialize cross-encoder model
        pass

    async def rerank(
        self,
        query: str,
        results: list[Dict[str, any]],
        limit: int = 10,
    ) -> list[Dict[str, any]]:
        """Rerank results using cross-encoder."""
        # For now, return results sorted by existing score
        # In production, use cross-encoder to rescore
        
        reranked = sorted(
            results,
            key=lambda r: r.get("fused_score", r.get("score", 0)),
            reverse=True,
        )
        
        return reranked[:limit]
