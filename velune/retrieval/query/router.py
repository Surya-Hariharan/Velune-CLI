"""Retrieval strategy router."""

from enum import Enum
from typing import Optional


class RetrievalStrategy(str, Enum):
    """Retrieval strategy options."""
    VECTOR_ONLY = "vector_only"
    LEXICAL_ONLY = "lexical_only"
    GRAPH_ONLY = "graph_only"
    HYBRID_VECTOR_LEXICAL = "hybrid_vector_lexical"
    HYBRID_ALL = "hybrid_all"


class RetrievalRouter:
    """Routes queries to appropriate retrieval strategy."""

    def __init__(self):
        pass

    def route(self, query: str, has_entity: bool = False) -> RetrievalStrategy:
        """Route query to retrieval strategy."""
        query_lower = query.lower()
        
        # If we have an entity ID, use graph retrieval
        if has_entity:
            return RetrievalStrategy.HYBRID_ALL
        
        # Code-specific queries benefit from lexical search
        if any(keyword in query_lower for keyword in ["function", "class", "method", "variable"]):
            return RetrievalStrategy.HYBRID_VECTOR_LEXICAL
        
        # Semantic queries benefit from vector search
        if any(keyword in query_lower for keyword in ["similar", "related", "like"]):
            return RetrievalStrategy.VECTOR_ONLY
        
        # Default to hybrid
        return RetrievalStrategy.HYBRID_VECTOR_LEXICAL
