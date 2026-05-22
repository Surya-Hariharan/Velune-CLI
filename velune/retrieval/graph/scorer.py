"""Graph proximity scoring."""

from typing import float, Dict


class GraphScorer:
    """Scores results based on graph proximity."""

    def score_by_distance(self, distance: int) -> float:
        """Score based on distance in graph."""
        if distance == 0:
            return 1.0
        return 1.0 / (distance + 1)

    def score_by_relationship_type(self, relationship_type: str) -> float:
        """Score based on relationship type."""
        # Certain relationships are more important
        important_relationships = ["contains", "implements", "extends"]
        
        if relationship_type in important_relationships:
            return 1.0
        else:
            return 0.7

    def score_by_centrality(self, entity_id: str, graph_store) -> float:
        """Score based on graph centrality."""
        # Count relationships
        relationships = graph_store.get_entity_relationships(entity_id)
        degree = len(relationships)
        
        # Normalize (assuming max degree of 100)
        return min(degree / 100, 1.0)
