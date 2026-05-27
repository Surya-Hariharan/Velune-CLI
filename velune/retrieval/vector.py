"""Vector retrieval layer using Qdrant client."""

import logging
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalSource

logger = logging.getLogger("velune.retrieval.vector")


class VectorRetriever:
    """Retrieves context from Qdrant vector database using dense embeddings."""

    def __init__(self, collection_name: str = "velune_symbols", location: str = ".velune/qdrant_local_store", client: QdrantClient | None = None) -> None:
        self.collection_name = collection_name
        self.location = location
        if client is not None:
            self.client = client
        elif location.startswith(".") or "/" in location or "\\" in location:
            self.client = QdrantClient(path=location)
        else:
            self.client = QdrantClient(location=location)
        self._detected_dimension: int | None = None

        # Proactively detect existing collection dimension if it exists
        try:
            collections = self.client.get_collections().collections
            existing = any(c.name == self.collection_name for c in collections)
            if existing:
                info = self.client.get_collection(self.collection_name)
                self._detected_dimension = info.config.params.vectors.size
                logger.info("Collection '%s' initialized with dimension %d", self.collection_name, self._detected_dimension)
        except Exception:
            pass

    def _ensure_collection_for_dimension(self, dim: int) -> None:
        """Create or verify collection for the given dimension."""
        if self._detected_dimension == dim:
            return  # Already configured

        try:
            collections = self.client.get_collections().collections
            existing = next((c for c in collections if c.name == self.collection_name), None)

            if existing:
                # Check if dimension matches
                info = self.client.get_collection(self.collection_name)
                existing_dim = info.config.params.vectors.size
                if existing_dim != dim:
                    logger.warning(
                        "Embedding dimension changed from %d to %d. "
                        "Recreating collection '%s'. Existing vectors lost.",
                        existing_dim, dim, self.collection_name
                    )
                    self.client.delete_collection(self.collection_name)
                    existing = None

            if not existing:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=qmodels.VectorParams(
                        size=dim,
                        distance=qmodels.Distance.COSINE
                    )
                )
                logger.info("Collection '%s' initialized with dimension %d", self.collection_name, dim)

            self._detected_dimension = dim
        except Exception as e:
            logger.error("Failed to ensure collection for dimension %d: %s", dim, e)

    def upsert(self, doc: RetrievalDocument) -> None:
        """Inserts or updates a document with its embedding in Qdrant."""
        if not doc.embedding:
            return

        dim = len(doc.embedding)
        self._ensure_collection_for_dimension(dim)

        try:
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    qmodels.PointStruct(
                        id=hash(doc.id) % (2**63 - 1),  # Map string ID to uint64
                        vector=doc.embedding,  # exact length, no padding
                        payload={
                            "doc_id": doc.id,
                            "content": doc.content,
                            "namespace": doc.namespace,
                            **doc.metadata
                        }
                    )
                ]
            )
        except Exception as e:
            logger.error("Failed to upsert document: %s", e)

    def retrieve(self, query_vector: list[float], top_k: int = 10, namespace: str | None = None) -> list[RetrievalHit]:
        """Queries Qdrant vector spaces and returns matching document hits."""
        if not self._detected_dimension:
            try:
                collections = self.client.get_collections().collections
                existing = any(c.name == self.collection_name for c in collections)
                if existing:
                    info = self.client.get_collection(self.collection_name)
                    self._detected_dimension = info.config.params.vectors.size
                else:
                    return []
            except Exception:
                return []

        if self._detected_dimension and len(query_vector) != self._detected_dimension:
            logger.error(
                "Query dimension mismatch — skipping vector retrieval. "
                "Query dimension %d != collection dimension %d.",
                len(query_vector), self._detected_dimension
            )
            return []

        hits: list[RetrievalHit] = []
        try:
            # Build filters
            query_filter = None
            if namespace:
                query_filter = qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="namespace",
                            match=qmodels.MatchValue(value=namespace)
                        )
                    ]
                )

            results = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=top_k
            ).points

            for rank, res in enumerate(results):
                payload = res.payload or {}
                doc = RetrievalDocument(
                    id=payload.get("doc_id", str(res.id)),
                    content=payload.get("content", ""),
                    namespace=payload.get("namespace", "default"),
                    metadata={k: v for k, v in payload.items() if k not in ("doc_id", "content", "namespace")}
                )
                hits.append(
                    RetrievalHit(
                        document=doc,
                        score=res.score,
                        source=RetrievalSource.VECTOR,
                        rank=rank + 1
                    )
                )
        except Exception as e:
            logger.error("Failed to retrieve matching documents from Qdrant: %s", e)

        return hits
