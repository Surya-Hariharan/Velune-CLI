"""Vector retrieval layer using Qdrant client."""

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalSource


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
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        """Ensures that the target Qdrant collection exists."""
        try:
            # We use standard 1536 dimension for embeddings (matching text-embedding-3-small or similar)
            # or 384 for standard local models. Let's make it highly dynamic.
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)

            if not exists:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=qmodels.VectorParams(
                        size=1536,  # Standard OpenAI dimension
                        distance=qmodels.Distance.COSINE
                    )
                )
        except Exception:
            # Fail silently or default to secondary mock configurations if client is unavailable
            pass

    def upsert(self, doc: RetrievalDocument) -> None:
        """Inserts or updates a document with its embedding in Qdrant."""
        if not doc.embedding:
            return

        # Ensure embedding matches collection dimensions (default to 1536)
        emb = doc.embedding
        if len(emb) < 1536:
            # Pad with zeros if size is smaller
            emb = emb + [0.0] * (1536 - len(emb))
        elif len(emb) > 1536:
            emb = emb[:1536]

        try:
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    qmodels.PointStruct(
                        id=hash(doc.id) % (2**63 - 1),  # Map string ID to uint64
                        vector=emb,
                        payload={
                            "doc_id": doc.id,
                            "content": doc.content,
                            "namespace": doc.namespace,
                            **doc.metadata
                        }
                    )
                ]
            )
        except Exception:
            pass

    def retrieve(self, query_vector: list[float], top_k: int = 10, namespace: str | None = None) -> list[RetrievalHit]:
        """Queries Qdrant vector spaces and returns matching document hits."""
        # Normalize vector dimension
        vector = query_vector
        if len(vector) < 1536:
            vector = vector + [0.0] * (1536 - len(vector))
        elif len(vector) > 1536:
            vector = vector[:1536]

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

            results = self.client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                query_filter=query_filter,
                limit=top_k
            )

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
        except Exception:
            pass

        return hits
