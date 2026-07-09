"""Vector retrieval layer using Qdrant client.

The Qdrant client and its compiled backend are resolved lazily so that wiring
this retriever during bootstrap costs nothing. A ``client_provider`` callable
is preferred over a concrete ``client``: it lets us share the semantic tier's
single Qdrant connection (two clients on the same local path would deadlock the
store) without forcing that connection to open at startup.
"""

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalSource

logger = logging.getLogger("velune.retrieval.vector")


def _qmodels() -> Any:
    """Lazily import qdrant http models (defers the qdrant_client import)."""
    from qdrant_client.http import models as qmodels

    return qmodels


def _point_id(doc_id: str) -> int:
    """Map a document ID to a stable uint64 Qdrant point ID.

    Must be deterministic *across processes*, not just within one: Python's
    built-in ``hash()`` is salted with a random seed per interpreter start
    (PYTHONHASHSEED), so the same ``doc_id`` previously mapped to a different
    point on every run — upserts could never overwrite their own prior point
    (silently doubling storage) and deletes computed from a re-derived ID
    could never find the point they meant to remove (silent orphan).
    """
    digest = hashlib.sha256(doc_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**63 - 1)


class VectorRetriever:
    """Retrieves context from Qdrant vector database using dense embeddings."""

    def __init__(
        self,
        collection_name: str = "velune_symbols",
        location: str = ".velune/qdrant_local_store",
        client: Any | None = None,
        client_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.collection_name = collection_name
        self.location = location
        self._client = client
        self._client_provider = client_provider
        self._client_resolved = client is not None
        self._detected_dimension: int | None = None

    @property
    def client(self) -> Any:
        """Resolve the Qdrant client on first use (provider > location)."""
        if not self._client_resolved:
            self._client_resolved = True
            if self._client_provider is not None:
                self._client = self._client_provider()
            else:
                from qdrant_client import QdrantClient

                loc = self.location
                if loc.startswith(".") or "/" in loc or "\\" in loc:
                    self._client = QdrantClient(path=loc)
                else:
                    self._client = QdrantClient(location=loc)
        return self._client

    def _ensure_collection_for_dimension(self, dim: int) -> None:
        """Create or verify collection for the given dimension."""
        if self._detected_dimension == dim:
            return  # Already configured

        client = self.client
        if client is None:
            return  # Degraded mode — no vector backend available
        qmodels = _qmodels()

        try:
            collections = client.get_collections().collections
            existing = next((c for c in collections if c.name == self.collection_name), None)

            if existing:
                # Check if dimension matches
                info = client.get_collection(self.collection_name)
                existing_dim = info.config.params.vectors.size
                if existing_dim != dim:
                    logger.warning(
                        "Embedding dimension changed from %d to %d. "
                        "Recreating collection '%s'. Existing vectors lost.",
                        existing_dim,
                        dim,
                        self.collection_name,
                    )
                    client.delete_collection(self.collection_name)
                    existing = None

            if not existing:
                client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
                )
                logger.info(
                    "Collection '%s' initialized with dimension %d", self.collection_name, dim
                )

            self._detected_dimension = dim
        except Exception as e:
            logger.error("Failed to ensure collection for dimension %d: %s", dim, e)

    def upsert(self, doc: RetrievalDocument) -> None:
        """Inserts or updates a document with its embedding in Qdrant."""
        if not doc.embedding:
            return

        dim = len(doc.embedding)
        self._ensure_collection_for_dimension(dim)

        client = self.client
        if client is None:
            return
        qmodels = _qmodels()
        try:
            client.upsert(
                collection_name=self.collection_name,
                points=[
                    qmodels.PointStruct(
                        id=_point_id(doc.id),
                        vector=doc.embedding,  # exact length, no padding
                        payload={
                            "doc_id": doc.id,
                            "content": doc.content,
                            "namespace": doc.namespace,
                            **doc.metadata,
                        },
                    )
                ],
            )
        except Exception as e:
            logger.error("Failed to upsert document: %s", e)

    def delete_by_ids(self, doc_ids: list[str]) -> None:
        """Removes the points for *doc_ids* (as passed to :meth:`upsert`).

        A no-op, not an error, when the collection doesn't exist yet or a
        given ID was never upserted — callers (e.g. incremental re-indexing
        reacting to deleted files) don't need to know whether anything was
        ever embedded for that document.
        """
        if not doc_ids:
            return
        client = self.client
        if client is None:
            return
        qmodels = _qmodels()
        try:
            client.delete(
                collection_name=self.collection_name,
                points_selector=qmodels.PointIdsList(points=[_point_id(d) for d in doc_ids]),
            )
        except Exception as e:
            logger.debug("Vector delete skipped (collection likely absent): %s", e)

    def retrieve(
        self, query_vector: list[float], top_k: int = 10, namespace: str | None = None
    ) -> list[RetrievalHit]:
        """Queries Qdrant vector spaces and returns matching document hits."""
        client = self.client
        if client is None:
            return []  # Degraded mode — no vector backend available
        qmodels = _qmodels()

        if not self._detected_dimension:
            try:
                collections = client.get_collections().collections
                existing = any(c.name == self.collection_name for c in collections)
                if existing:
                    info = client.get_collection(self.collection_name)
                    self._detected_dimension = info.config.params.vectors.size
                else:
                    return []
            except Exception:
                return []

        if self._detected_dimension and len(query_vector) != self._detected_dimension:
            logger.error(
                "Query dimension mismatch — skipping vector retrieval. "
                "Query dimension %d != collection dimension %d.",
                len(query_vector),
                self._detected_dimension,
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
                            key="namespace", match=qmodels.MatchValue(value=namespace)
                        )
                    ]
                )

            results = client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=top_k,
            ).points

            for rank, res in enumerate(results):
                payload = res.payload or {}
                doc = RetrievalDocument(
                    id=payload.get("doc_id", str(res.id)),
                    content=payload.get("content", ""),
                    namespace=payload.get("namespace", "default"),
                    metadata={
                        k: v
                        for k, v in payload.items()
                        if k not in ("doc_id", "content", "namespace")
                    },
                )
                hits.append(
                    RetrievalHit(
                        document=doc, score=res.score, source=RetrievalSource.VECTOR, rank=rank + 1
                    )
                )
        except Exception as e:
            logger.error("Failed to retrieve matching documents from Qdrant: %s", e)

        return hits
