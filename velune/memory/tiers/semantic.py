"""Semantic Memory Tier (Tier 3).

Qdrant-backed semantic store managing dense code symbol embeddings,
summaries, and payload-filtered contextual searches.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

logger = logging.getLogger("velune.memory.tiers.semantic")


class SemanticMemoryTier:
    """Tier 3: Semantic store using Qdrant (supports in-process local storage and server modes)."""

    def __init__(
        self,
        location: str = ":memory:",
        url: str | None = None,
        api_key: str | None = None,
        path: str | None = None,
    ) -> None:
        """
        Initialize Qdrant client connection.
        If url/api_key is set, connects to remote server.
        If path is set, runs in-process local storage with SQLite persistence.
        Otherwise, runs in-process volatile memory client (:memory:).
        """
        if url:
            logger.debug("Initializing Qdrant remote client at %s", url)
            self.client = QdrantClient(url=url, api_key=api_key)
        elif path:
            logger.debug("Initializing Qdrant in-process local storage at %s", path)
            self.client = QdrantClient(path=path)
        else:
            logger.debug("Initializing Qdrant volatile in-memory client (:memory:)")
            self.client = QdrantClient(location=location)

    def create_collection(
        self,
        collection_name: str,
        vector_size: int = 1536,
        distance_metric: str = "Cosine",
    ) -> None:
        """Create a new collection if it does not already exist."""
        try:
            # Check if exists
            collections = self.client.get_collections().collections
            exists = any(c.name == collection_name for c in collections)

            if not exists:
                metric = qmodels.Distance.COSINE
                if distance_metric.lower() == "euclidean":
                    metric = qmodels.Distance.EUCLID
                elif distance_metric.lower() == "dot":
                    metric = qmodels.Distance.DOT

                self.client.create_collection(
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
            self.client.upsert(
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
            results = self.client.query_points(
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
        except UnexpectedResponse as e:
            logger.error("Qdrant query returned unexpected response: %s", e)
            return []
        except Exception as e:
            logger.error("Semantic search failure on %s: %s", collection_name, e)
            return []

    def delete_points(self, collection_name: str, ids: list[int | str]) -> None:
        """Delete specific vectors by their identifier."""
        try:
            cleaned_ids = [self._clean_id(p_id) for p_id in ids]
            self.client.delete(
                collection_name=collection_name,
                points_selector=qmodels.PointIdsList(points=cleaned_ids),
            )
        except Exception as e:
            logger.error("Failed to delete points in %s: %s", collection_name, e)

    def delete_by_payload(self, collection_name: str, payload_filter: dict[str, Any]) -> None:
        """Delete points matching a payload filter."""
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
            self.client.delete(
                collection_name=collection_name,
                points_selector=qmodels.FilterSelector(filter=q_filter),
            )
            logger.debug("Successfully deleted points matching filter %s from %s", payload_filter, collection_name)
        except Exception as e:
            logger.error("Failed to delete points by payload in %s: %s", collection_name, e)
