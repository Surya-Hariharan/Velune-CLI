"""Fusion orchestrator merging BM25, Qdrant vectors, and dependency graphs."""

from typing import Any

from velune.kernel.registry import ComponentRegistry
from velune.retrieval.graph import GraphRetriever
from velune.retrieval.keyword import BM25Retriever
from velune.retrieval.reranker import ContextReranker
from velune.retrieval.schemas import RetrievalHit, RetrievalQuery, RetrievalResult
from velune.retrieval.vector import VectorRetriever


class HybridRetriever:
    """Orchestrates fusion retrieval, combining Lexical, Vector, and Graph traversals.

    Primary interface: await retrieve(). search() is sync-only.
    """

    def __init__(self, location: str = ":memory:", client: Any | None = None) -> None:
        self.registry = ComponentRegistry()
        self.vector_retriever = VectorRetriever(location=location, client=client)
        self.lexical_retriever = BM25Retriever()
        self.graph_retriever = GraphRetriever()
        self.reranker = ContextReranker()

    def add_documents(self, docs: list[Any]) -> None:
        """Adds and indexes documents in both vector and lexical subsystems.

        All documents must have a pre-computed embedding.
        """
        # Index in Lexical (BM25)
        self.lexical_retriever.add_documents(docs)

        # Index in Vector (Qdrant)
        for doc in docs:
            # Require embedding to be pre-computed (make embedding field required, not optional)
            if not doc.embedding:
                raise ValueError(
                    f"Document {doc.id} must have a pre-computed embedding. "
                    "All callers of add_documents() must pre-compute embeddings using await before calling."
                )
            self.vector_retriever.upsert(doc)

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Performs full hybrid retrieval, merges candidate pools, and reranks."""
        lexical_hits: list[RetrievalHit] = []
        vector_hits: list[RetrievalHit] = []
        graph_hits: list[RetrievalHit] = []

        # 1. Execute Lexical search (BM25)
        if query.lexical_weight > 0.0:
            try:
                lexical_hits = self.lexical_retriever.retrieve(
                    query.text, top_k=query.top_k, namespace=query.namespace
                )
            except Exception:
                pass

        # 2. Execute Vector search (Qdrant)
        if query.vector_weight > 0.0:
            try:
                # Generate embedding for the query
                emb = await self._generate_embedding_async(query.text)
                vector_hits = self.vector_retriever.retrieve(
                    emb, top_k=query.top_k, namespace=query.namespace
                )
            except Exception:
                pass

        # 3. Execute Graph traversal search
        # If we have hits from lexical or vector search, traverse neighboring file links
        if query.graph_weight > 0.0:
            seed_nodes = []
            # Gather file path candidates
            for hit in lexical_hits[:3] + vector_hits[:3]:
                path = hit.document.metadata.get("path")
                if path:
                    seed_nodes.append(path)
                name = hit.document.metadata.get("name")
                if name:
                    seed_nodes.append(name)

            for node in set(seed_nodes):
                try:
                    gh = self.graph_retriever.retrieve(node, depth=1, top_k=5)
                    graph_hits.extend(gh)
                except Exception:
                    pass

        # 4. Fusion and Deduplication
        merged_hits_map: dict[str, RetrievalHit] = {}

        # Helper to blend weights into score
        def merge_hit(hit: RetrievalHit, weight: float) -> None:
            doc_id = hit.document.id
            weighted_score = hit.score * weight

            if doc_id in merged_hits_map:
                # Combine scores from multiple search strategies
                existing = merged_hits_map[doc_id]
                existing.score += weighted_score
            else:
                hit.score = weighted_score
                merged_hits_map[doc_id] = hit

        for h in lexical_hits:
            merge_hit(h, query.lexical_weight)
        for h in vector_hits:
            merge_hit(h, query.vector_weight)
        for h in graph_hits:
            merge_hit(h, query.graph_weight)

        all_hits = list(merged_hits_map.values())

        # 5. Rerank final combined candidates
        reranked_hits = self.reranker.rerank(query.text, all_hits)

        return RetrievalResult(
            query=query,
            hits=reranked_hits[:query.top_k],
            strategy="hybrid-fusion-reranked"
        )

    def search_sync(self, query: RetrievalQuery) -> RetrievalResult:
        """Synchronous retrieval. DEPRECATED: use await retrieve() in async contexts.
        
        This method creates a new event loop for synchronous callers. Do NOT call 
        from within a running event loop — use 'await self.retrieve(query)' instead.
        
        Raises:
            RuntimeError: If called from within a running event loop.
        """
        import asyncio
        import warnings
        warnings.warn(
            "HybridRetriever.search_sync() is deprecated and will be removed in a future version. "
            "Use 'await retriever.retrieve(query)' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "HybridRetriever.search_sync() cannot be called from an async context. "
                "Use 'await retriever.retrieve(query)' instead."
            )
        except RuntimeError as e:
            if "cannot be called" in str(e):
                raise
            # No running loop — safe to use asyncio.run()
        
        return asyncio.run(self.retrieve(query))

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        """Synchronous interface. Do NOT call from within a running event loop.
        Use await retrieve() instead from async contexts."""
        import asyncio
        import warnings
        try:
            loop = asyncio.get_running_loop()
            warnings.warn(
                "HybridRetriever.search() called from async context. "
                "Use 'await retriever.retrieve()' instead.",
                RuntimeWarning, stacklevel=2
            )
        except RuntimeError:
            pass  # No running loop, safe to use asyncio.run()
        return asyncio.run(self.retrieve(query))

    def _deterministic_fallback_embedding(self, text: str) -> list[float]:
        """Sophisticated deterministic fallback embedding vector."""
        res = [0.0] * 1536
        for idx, char in enumerate(text[:300]):
            res[idx % 1536] += ord(char) / 256.0
        return res

    async def _generate_embedding_async(self, text: str) -> list[float]:
        """Generates embedding asynchronously using the registered ModelProvider, or falls back to a deterministic vector.

        INTERNAL ONLY.
        """
        try:
            from velune.kernel.registry import get_container
            container = get_container()
            if container.has("runtime.provider_registry"):
                provider_registry = container.get("runtime.provider_registry")
                config = container.get("runtime.config") if container.has("runtime.config") else None

                provider_name = "openai"
                if config and hasattr(config, "providers") and config.providers:
                    provider_name = config.providers.default_provider

                provider = provider_registry.get(provider_name)
                if provider:
                    model_id = "text-embedding-3-small"
                    if provider_name == "ollama":
                        model_id = "nomic-embed-text"

                    res = await provider.embed([text], model_id=model_id)
                    emb = res[0] if res else None
                    if emb:
                        return emb
        except Exception as e:
            import logging
            logging.getLogger("velune.retrieval.hybrid").warning(
                "Failed to generate embedding using ModelProvider: %s. Falling back to deterministic embedding.", e
            )

        return self._deterministic_fallback_embedding(text)


