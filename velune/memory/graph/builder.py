"""Entity and relationship extraction."""

from typing import list, Dict, Any
from velune.memory.graph.store import GraphMemoryStore


class GraphBuilder:
    """Builds graph memory from experiences."""

    def __init__(self, store: GraphMemoryStore):
        self.store = store

    def extract_from_code(
        self,
        code: str,
        file_path: str,
    ) -> None:
        """Extract entities and relationships from code."""
        # Simple extraction for demonstration
        # In production, use AST parsing
        
        # Add file entity
        self.store.add_entity(
            entity_id=file_path,
            entity_type="file",
            name=file_path,
            properties={"language": self._detect_language(file_path)},
        )

        # Extract function entities
        lines = code.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("def "):
                func_name = line[4:].split("(")[0].strip()
                func_id = f"{file_path}::{func_name}"
                self.store.add_entity(
                    entity_id=func_id,
                    entity_type="function",
                    name=func_name,
                    properties={"file": file_path},
                )
                # Add contains relationship
                self.store.add_relationship(
                    relationship_id=f"{file_path}_contains_{func_id}",
                    source_entity_id=file_path,
                    target_entity_id=func_id,
                    relationship_type="contains",
                    properties={},
                )

    def extract_from_conversation(
        self,
        conversation: list[dict],
    ) -> None:
        """Extract entities and relationships from conversation."""
        # Extract mentioned entities
        for turn in conversation:
            content = turn.get("content", "")
            # Simple entity extraction
            words = content.split()
            for word in words:
                if word[0].isupper() and len(word) > 1:
                    # Potentially an entity
                    entity_id = word.lower()
                    if not self.store.get_entity(entity_id):
                        self.store.add_entity(
                            entity_id=entity_id,
                            entity_type="mentioned",
                            name=word,
                            properties={"context": content},
                        )

    def _detect_language(self, file_path: str) -> str:
        """Detect programming language from file path."""
        if file_path.endswith(".py"):
            return "python"
        elif file_path.endswith(".js") or file_path.endswith(".ts"):
            return "javascript"
        elif file_path.endswith(".rs"):
            return "rust"
        elif file_path.endswith(".go"):
            return "go"
        else:
            return "unknown"
