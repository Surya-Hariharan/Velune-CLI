"""Long-term semantic memory store (vector-backed)."""

from typing import Optional, list
import chromadb
from chromadb.config import Settings
from velune.core.types import MemoryRecord, MemoryType, MemoryQuery
from velune.core.errors import MemoryStoreError


class SemanticMemoryStore:
    """Vector-backed semantic memory store using ChromaDB."""

    def __init__(self, collection_name: str = "velune_semantic"):
        self.collection_name = collection_name
        self.client = chromadb.PersistentClient(
            path=".velune/memory/semantic",
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, record: MemoryRecord) -> None:
        """Add a record to semantic memory."""
        if record.memory_type != MemoryType.SEMANTIC:
            raise MemoryStoreError("Semantic memory store only accepts SEMANTIC type records")
        
        if not record.embedding:
            raise MemoryStoreError("Semantic memory records must have embeddings")
        
        self.collection.add(
            ids=[record.id],
            embeddings=[record.embedding],
            documents=[record.content],
            metadatas=[
                {
                    "importance": record.importance,
                    "created_at": record.created_at.isoformat(),
                    **record.metadata,
                }
            ],
        )

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Get a record from semantic memory."""
        results = self.collection.get(ids=[record_id])
        if not results["ids"]:
            return None
        
        return self._result_to_record(results, 0)

    def query(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Query semantic memory."""
        results = self.collection.query(
            query_embeddings=[query.query_embedding] if hasattr(query, "query_embedding") else None,
            query_texts=[query.query_text] if not hasattr(query, "query_embedding") else None,
            n_results=query.limit,
            where={
                "importance": {"$gte": query.min_importance},
            } if query.min_importance > 0 else None,
        )
        
        if not results["ids"]:
            return []
        
        return [self._result_to_record(results, i) for i in range(len(results["ids"][0]))]

    def delete(self, record_id: str) -> None:
        """Delete a record from semantic memory."""
        self.collection.delete(ids=[record_id])

    def _result_to_record(self, results: dict, index: int) -> MemoryRecord:
        """Convert a query result to a MemoryRecord."""
        from datetime import datetime
        
        return MemoryRecord(
            id=results["ids"][0][index],
            memory_type=MemoryType.SEMANTIC,
            content=results["documents"][0][index],
            embedding=results["embeddings"][0][index] if results["embeddings"] else None,
            importance=results["metadatas"][0][index].get("importance", 0.5),
            access_count=0,
            last_accessed=datetime.now(),
            created_at=datetime.fromisoformat(
                results["metadatas"][0][index].get("created_at", datetime.now().isoformat())
            ),
            expires_at=None,
            metadata={k: v for k, v in results["metadatas"][0][index].items() if k not in ["importance", "created_at"]},
        )
