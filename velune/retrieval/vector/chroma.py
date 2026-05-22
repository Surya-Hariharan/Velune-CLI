"""ChromaDB adapter."""

import chromadb
from chromadb.config import Settings
from typing import list, Optional, Dict, Any
from velune.retrieval.vector.store import VectorStore


class ChromaVectorStore(VectorStore):
    """ChromaDB implementation of vector store."""

    def __init__(self, collection_name: str = "velune_retrieval"):
        self.collection_name = collection_name
        self.client = chromadb.PersistentClient(
            path=".velune/retrieval/vector",
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    async def add(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: Optional[list[Dict[str, Any]]] = None,
    ) -> None:
        """Add documents to ChromaDB."""
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    async def query(
        self,
        query_embedding: list[float],
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Query ChromaDB."""
        return self.collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where,
        )

    async def delete(self, ids: list[str]) -> None:
        """Delete documents from ChromaDB."""
        self.collection.delete(ids=ids)

    async def get(self, ids: list[str]) -> Dict[str, Any]:
        """Get documents by IDs from ChromaDB."""
        return self.collection.get(ids=ids)
