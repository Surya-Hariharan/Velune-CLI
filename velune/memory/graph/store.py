"""Graphiti-backed graph memory store."""

from typing import Optional, Dict, Any
from velune.core.types import MemoryRecord, MemoryType
from velune.core.errors import MemoryStoreError


class GraphMemoryStore:
    """Graph memory store using Graphiti."""

    def __init__(self):
        # Graphiti integration would go here
        # For now, we'll use a simple in-memory structure
        self._entities: Dict[str, Dict[str, Any]] = {}
        self._relationships: Dict[str, Dict[str, Any]] = {}

    def add_entity(
        self,
        entity_id: str,
        entity_type: str,
        name: str,
        properties: Dict[str, Any],
    ) -> None:
        """Add an entity to the graph."""
        self._entities[entity_id] = {
            "type": entity_type,
            "name": name,
            "properties": properties,
        }

    def add_relationship(
        self,
        relationship_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str,
        properties: Dict[str, Any],
    ) -> None:
        """Add a relationship to the graph."""
        self._relationships[relationship_id] = {
            "source": source_entity_id,
            "target": target_entity_id,
            "type": relationship_type,
            "properties": properties,
        }

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Get an entity from the graph."""
        return self._entities.get(entity_id)

    def get_relationship(self, relationship_id: str) -> Optional[Dict[str, Any]]:
        """Get a relationship from the graph."""
        return self._relationships.get(relationship_id)

    def find_entities_by_type(self, entity_type: str) -> list[Dict[str, Any]]:
        """Find entities by type."""
        return [
            {"id": eid, **data}
            for eid, data in self._entities.items()
            if data["type"] == entity_type
        ]

    def find_relationships_by_type(self, relationship_type: str) -> list[Dict[str, Any]]:
        """Find relationships by type."""
        return [
            {"id": rid, **data}
            for rid, data in self._relationships.items()
            if data["type"] == relationship_type
        ]

    def get_entity_relationships(self, entity_id: str) -> list[Dict[str, Any]]:
        """Get all relationships for an entity."""
        return [
            {"id": rid, **data}
            for rid, data in self._relationships.items()
            if data["source"] == entity_id or data["target"] == entity_id
        ]
