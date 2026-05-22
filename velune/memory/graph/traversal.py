"""Graph traversal for retrieval."""

from typing import list, Dict, Any, Optional
from velune.memory.graph.store import GraphMemoryStore


class GraphTraversal:
    """Traverses graph memory for retrieval."""

    def __init__(self, store: GraphMemoryStore):
        self.store = store

    def find_related_entities(
        self,
        entity_id: str,
        max_depth: int = 2,
    ) -> list[Dict[str, Any]]:
        """Find entities related to a given entity."""
        visited = set()
        related = []
        self._dfs(entity_id, 0, max_depth, visited, related)
        return related

    def _dfs(
        self,
        entity_id: str,
        depth: int,
        max_depth: int,
        visited: set,
        related: list,
    ) -> None:
        """Depth-first search for related entities."""
        if depth >= max_depth or entity_id in visited:
            return
        
        visited.add(entity_id)
        
        relationships = self.store.get_entity_relationships(entity_id)
        for rel in relationships:
            source = rel["source"]
            target = rel["target"]
            
            next_entity = target if source == entity_id else source
            
            if next_entity not in visited:
                entity = self.store.get_entity(next_entity)
                if entity:
                    related.append({
                        "id": next_entity,
                        "depth": depth + 1,
                        "relationship": rel["type"],
                        **entity,
                    })
                    self._dfs(next_entity, depth + 1, max_depth, visited, related)

    def find_path(
        self,
        source_id: str,
        target_id: str,
        max_length: int = 5,
    ) -> Optional[list[Dict[str, Any]]]:
        """Find a path between two entities."""
        from collections import deque
        
        queue = deque([(source_id, [])])
        visited = {source_id}
        
        while queue:
            current, path = queue.popleft()
            
            if current == target_id:
                return path
            
            if len(path) >= max_length:
                continue
            
            relationships = self.store.get_entity_relationships(current)
            for rel in relationships:
                next_entity = rel["target"] if rel["source"] == current else rel["source"]
                
                if next_entity not in visited:
                    visited.add(next_entity)
                    new_path = path + [
                        {
                            "from": current,
                            "to": next_entity,
                            "relationship": rel["type"],
                        }
                    ]
                    queue.append((next_entity, new_path))
        
        return None

    def find_entities_by_property(
        self,
        property_name: str,
        property_value: Any,
    ) -> list[Dict[str, Any]]:
        """Find entities by property."""
        matching = []
        for entity_id, data in self.store._entities.items():
            if property_name in data["properties"]:
                if data["properties"][property_name] == property_value:
                    matching.append({"id": entity_id, **data})
        return matching
