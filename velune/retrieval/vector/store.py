"""Vector store abstraction."""

from abc import ABC, abstractmethod
from typing import list, Optional, Dict, Any


class VectorStore(ABC):
    """Abstract vector store."""

    @abstractmethod
    async def add(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: Optional[list[Dict[str, Any]]] = None,
    ) -> None:
        """Add documents to the vector store."""
        pass

    @abstractmethod
    async def query(
        self,
        query_embedding: list[float],
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Query the vector store."""
        pass

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        """Delete documents from the vector store."""
        pass

    @abstractmethod
    async def get(self, ids: list[str]) -> Dict[str, Any]:
        """Get documents by IDs."""
        pass
