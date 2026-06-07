"""Semantic Memory Tier (Tier 3).

Qdrant-backed semantic store managing dense code symbol embeddings,
summaries, and payload-filtered contextual searches.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

# NOTE: qdrant_client (and its compiled local-mode backend) is imported lazily
# inside _ensure_client(). Importing it at module load — and constructing the
# client — was a multi-second cost on the startup path, especially with the
# store on a cloud-synced drive. Vectors are needed only once a memory/retrieval
# operation actually runs, so we defer both the import and the connection.

logger = logging.getLogger("velune.memory.tiers.semantic")


def _qmodels() -> Any:
    """Lazily import qdrant http models."""
    from qdrant_client.http import models as qmodels
    return qmodels


class SemanticMemoryTier:
    """Tier 3: Semantic store using Qdrant (lazy-initialized, degradable).

    The Qdrant client is created on first access rather than at construction.
    This keeps the vector backend off the critical startup path and lets the
    rest of the system boot even when the vector store is unavailable. Set
    ``VELUNE_SKIP_QDRANT=1`` to force a no-op degraded mode (useful for fast dev
    iteration); all operations then become safe no-ops and searches return ``[]``.
    """

    def __init__(
        self,
        location: str = ":memory:",
        url: str | None = None,
        api_key: str | None = None,
        path: str | None = None,
    ) -> None:
        self._location = location
        self._url = url
        self._api_key = api_key
        self._path = path
        self._client: Any = None
        self._degraded = os.environ.get("VELUNE_SKIP_QDRANT", "").lower() in ("1", "true", "yes")
        if self._degraded:
            logger.warning("VELUNE_SKIP_QDRANT set — semantic memory running in degraded (no-op) mode.")

    def _ensure_client(self) -> Any:
        """Create (once) and return the Qdrant client, or None in degraded mode."""
        if self._degraded:
            return None
        if self._client is not None:
            return self._client
        try:
            from qdrant_client import QdrantClient
            if self._url:
                logger.debug("Initializing Qdrant remote client at %s", self._url)
                self._client = QdrantClient(url=self._url, api_key=self._api_key)
            elif self._path:
                logger.debug("Initializing Qdrant in-process local storage at %s", self._path)
                self._client = QdrantClient(path=self._path)
            else:
                logger.debug("Initializing Qdrant volatile in-memory client (:memory:)")
                self._client = QdrantClient(location=self._location)
        except Exception as exc:
            # Degrade gracefully rather than crashing the whole runtime.
            logger.error("Qdrant initialization failed; semantic memory degraded: %s", exc)
            self._degraded = True
            return None
        return self._client

    @property
    def client(self) -> Any:
        """Backward-compatible accessor; triggers lazy initialization."""
        return self._ensure_client()

    def create_collection(
        self,
        collection_name: str,
        vector_size: int = 1536,
        distance_metric: str = "Cosine",
    ) -> None:
        """Create a new collection if it does not already exist."""
        client = self._ensure_client()
        if client is None:
            return  # Degraded mode
        qmodels = _qmodels()
        try:
            # Check if exists
            collections = client.get_collections().collections
            exists = any(c.name == collection_name for c in collections)

            if not exists:
                metric = qmodels.Distance.COSINE
                if distance_metric.lower() == "euclidean":
                    metric = qmodels.Distance.EUCLID
                elif distance_metric.lower() == "dot":
                    metric = qmodels.Distance.DOT

                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=qmodels.VectorParams(
                        size=vector_size,
                        distance=metric,
                    ),
                )
                logger.debug("Created Qdrant collection: %s", collection_name)
        except Exception as e:
            logger.error("Failed to create collection %s: %s", collection_name, e)

    def _clean_id(self, p_id: int | str) -> int | str:
        """Ensure the point ID is a valid Qdrant ID: a 64-bit int or a valid UUID string."""
        if isinstance(p_id, int):
            return p_id
        if isinstance(p_id, str):
            try:
                # Check if it is a valid UUID
                uuid.UUID(p_id)
                return p_id
            except ValueError:
                # Deterministic UUID string from arbitrary string
                return str(uuid.uuid5(uuid.NAMESPACE_DNS, p_id))
        return hash(p_id) % (2**63 - 1)

    def upsert_points(
        self,
        collection_name: str,
        ids: list[int | str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> None:
        """Upsert structural code or memory embedding points into the collection."""
        client = self._ensure_client()
        if client is None:
            return  # Degraded mode
        qmodels = _qmodels()
        if vectors:
            dims = {len(v) for v in vectors}
            if len(dims) > 1:
                raise ValueError(f"Mixed embedding dimensions in batch: {dims}")

        points = []
        for i, (p_id, vec, pay) in enumerate(zip(ids, vectors, payloads)):
            # Ensure unique IDs are formatted correctly
            point_id = self._clean_id(p_id) if p_id is not None else i
            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector=vec,
                    payload=pay,
                )
            )

        try:
            client.upsert(
                collection_name=collection_name,
                points=points,
            )
            logger.debug("Successfully upserted %d points into %s", len(points), collection_name)
        except Exception as e:
            logger.error("Failed upserting vectors in collection %s: %s", collection_name, e)

    def search_similarity(
        self,
        collection_name: str,
        query_vector: list[float],
        limit: int = 5,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query vector similarities under optional key-value metadata payload filter matching.
        """
        client = self._ensure_client()
        if client is None:
            return []  # Degraded mode
        qmodels = _qmodels()
        q_filter = None
        if payload_filter:
            conditions = []
            for key, val in payload_filter.items():
                conditions.append(
                    qmodels.FieldCondition(
                        key=key,
                        match=qmodels.MatchValue(value=val),
                    )
                )
            q_filter = qmodels.Filter(must=conditions)

        try:
            results = client.query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=limit,
                query_filter=q_filter,
            ).points

            output = []
            for item in results:
                output.append({
                    "id": item.id,
                    "score": item.score,
                    "payload": item.payload or {},
                })
            return output
        except Exception as e:
            logger.error("Semantic search failure on %s: %s", collection_name, e)
            return []

    def delete_points(self, collection_name: str, ids: list[int | str]) -> None:
        """Delete specific vectors by their identifier."""
        client = self._ensure_client()
        if client is None:
            return  # Degraded mode
        qmodels = _qmodels()
        try:
            cleaned_ids = [self._clean_id(p_id) for p_id in ids]
            client.delete(
                collection_name=collection_name,
                points_selector=qmodels.PointIdsList(points=cleaned_ids),
            )
        except Exception as e:
            logger.error("Failed to delete points in %s: %s", collection_name, e)

    def delete_by_payload(self, collection_name: str, payload_filter: dict[str, Any]) -> None:
        """Delete points matching a payload filter."""
        client = self._ensure_client()
        if client is None:
            return  # Degraded mode
        qmodels = _qmodels()
        try:
            conditions = []
            for key, val in payload_filter.items():
                conditions.append(
                    qmodels.FieldCondition(
                        key=key,
                        match=qmodels.MatchValue(value=val),
                    )
                )
            q_filter = qmodels.Filter(must=conditions)
            client.delete(
                collection_name=collection_name,
                points_selector=qmodels.FilterSelector(filter=q_filter),
            )
            logger.debug("Successfully deleted points matching filter %s from %s", payload_filter, collection_name)
        except Exception as e:
            logger.error("Failed to delete points by payload in %s: %s", collection_name, e)
