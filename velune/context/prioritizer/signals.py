"""Relevance signal extraction."""

from typing import Dict, Any
from velune.core.types import ContextChunk


class SignalExtractor:
    """Extracts relevance signals from context chunks."""

    def extract_signals(self, chunk: ContextChunk, query: str) -> Dict[str, Any]:
        """Extract relevance signals from a chunk."""
        signals = {}
        
        # Text similarity signal
        signals["text_similarity"] = self._calculate_text_similarity(chunk.content, query)
        
        # Recency signal
        signals["recency"] = self._calculate_recency(chunk.timestamp)
        
        # Source importance signal
        signals["source_importance"] = self._calculate_source_importance(chunk.source)
        
        # Length signal (prefer concise but informative)
        signals["length_score"] = self._calculate_length_score(chunk.content)
        
        # Metadata signals
        signals["metadata_signals"] = self._extract_metadata_signals(chunk.metadata)
        
        return signals

    def _calculate_text_similarity(self, text: str, query: str) -> float:
        """Calculate text similarity using simple overlap."""
        text_words = set(text.lower().split())
        query_words = set(query.lower().split())
        
        if not query_words:
            return 0.0
        
        overlap = len(text_words & query_words)
        return overlap / len(query_words)

    def _calculate_recency(self, timestamp: float) -> float:
        """Calculate recency score (more recent = higher)."""
        import time
        age_seconds = time.time() - timestamp
        
        # Decay over time: 1.0 for recent, 0.0 for very old
        half_life = 3600  # 1 hour
        return 0.5 ** (age_seconds / half_life)

    def _calculate_source_importance(self, source: str) -> float:
        """Calculate source importance score."""
        # Certain sources are more important
        important_sources = ["user_input", "code", "documentation"]
        
        if any(imp in source.lower() for imp in important_sources):
            return 1.0
        else:
            return 0.5

    def _calculate_length_score(self, text: str) -> float:
        """Calculate length score (prefer moderate length)."""
        length = len(text)
        
        # Optimal range: 100-1000 characters
        if 100 <= length <= 1000:
            return 1.0
        elif length < 100:
            return length / 100
        else:
            return max(0.5, 1.0 - (length - 1000) / 5000)

    def _extract_metadata_signals(self, metadata: Dict[str, Any]) -> float:
        """Extract signals from metadata."""
        if not metadata:
            return 0.0
        
        # Check for importance flags
        if metadata.get("important", False):
            return 1.0
        
        # Check for relevance flags
        if metadata.get("relevant", False):
            return 0.8
        
        return 0.5
