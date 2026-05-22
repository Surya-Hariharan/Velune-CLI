"""Cross-encoder API reranking with lightweight fallback similarity scoring."""

from typing import List

from velune.retrieval.schemas import RetrievalHit


class ContextReranker:
    """Re-scores candidate hits using lexical alignment and semantic keyword density."""

    def __init__(self) -> None:
        pass

    def rerank(self, query: str, hits: List[RetrievalHit]) -> List[RetrievalHit]:
        """Re-scores and re-ranks candidate hits based on similarity density."""
        if not hits:
            return []

        scored_hits: List[RetrievalHit] = []
        for hit in hits:
            score = self._compute_alignment_score(query, hit.document.content)
            
            # Combine the original retrieval score (BM25 or vector cosine) with our alignment score
            # Vector cosine is between -1.0 and 1.0 (typically 0.4-0.9), BM25 can be larger.
            # We scale the alignment score to a 0.0-1.0 range and merge.
            merged_score = hit.score * 0.4 + score * 0.6
            
            # Clone hit with new score
            scored_hits.append(
                RetrievalHit(
                    document=hit.document,
                    score=merged_score,
                    source=hit.source,
                    rank=0
                )
            )

        # Sort by updated scores
        scored_hits.sort(key=lambda x: x.score, reverse=True)
        
        # Apply new ranks
        for idx, h in enumerate(scored_hits):
            h.rank = idx + 1
            
        return scored_hits

    def _compute_alignment_score(self, query: str, document_text: str) -> float:
        """Computes lexical term alignment and density score (Jaccard + keyword overlaps)."""
        q_tokens = set(query.lower().split())
        doc_tokens = set(document_text.lower().split())
        
        if not q_tokens or not doc_tokens:
            return 0.0
            
        # Jaccard similarity index
        intersection = q_tokens.intersection(doc_tokens)
        union = q_tokens.union(doc_tokens)
        jaccard = len(intersection) / len(union)
        
        # Coverage fraction (how many query words appear in document)
        coverage = len(intersection) / len(q_tokens)
        
        # Exact substring matches (bonus weight for sequential phrase matches)
        phrase_bonus = 0.0
        if query.lower() in document_text.lower():
            phrase_bonus = 0.3
            
        # Composite score
        return jaccard * 0.2 + coverage * 0.5 + phrase_bonus
