"""Graph-traversal retrieval."""

from typing import list, Dict, Optional
from velune.memory.graph.store import GraphMemoryStore
from velune.memory.graph.traversal import GraphTraversal


class GraphRetriever:
    """Retriever using graph traversal."""

    def __init__(self, graph_store: GraphMemoryStore):
        self.graph_store = graph_store
        self.traversal = GraphTraversal(graph_store)

    def retrieve(
        self,
        entity_id: str,
        max_depth: int = 2,
        limit: int = 10,
    ) -> list[Dict[str, any]]:
        """Retrieve related entities via graph traversal."""
        related = self.traversal.find_related_entities(entity_id, max_depth)
        
        formatted_results = []
        for entity in related[:limit]:
            formatted_results.append({
                "id": entity["id"],
                "type": entity.get("type", "unknown"),
                "name": entity.get("name", ""),
                "depth": entity.get("depth", 0),
                "relationship": entity.get("relationship", ""),
                "score": 1.0 / (entity.get("depth", 1) + 1),  # Closer = higher score
                "retrieval_method": "graph",
            })
        
        return formatted_results

    def retrieve_by_property(
        self,
        property_name: str,
        property_value: any,
        limit: int = 10,
    ) -> list[Dict[str, any]]:
        """Retrieve entities by property."""
        entities = self.traversal.find_entities_by_property(property_name, property_value)
        
        formatted_results = []
        for entity in entities[:limit]:
            formatted_results.append({
                "id": entity["id"],
                "type": entity.get("type", "unknown"),
                "name": entity.get("name", ""),
                "score": 1.0,
                "retrieval_method": "graph",
            })
        
        return formatted_results
