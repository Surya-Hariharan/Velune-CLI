"""Fast path retriever: ANN search + local graph expansion (< 200ms)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger("velune.retrieval.fast_path")


class FastPathRetriever:
    """Fast retrieval using ANN search and local graph expansion."""

    # Limits
    ANN_RESULTS_LIMIT = 10
    LOCAL_GRAPH_HOP_DEPTH = 1
    TOP_RESULTS_FOR_EXPANSION = 3
    TARGET_LATENCY_MS = 200

    def __init__(
        self,
        semantic_memory: Any,
        symbol_registry: Any,
        embedding_pipeline: Any,
        import_graph: Any,
        embedding_cache: dict[str, Any] | None = None,
    ) -> None:
        """Initialize fast path retriever.

        Parameters
        ----------
        semantic_memory:
            SemanticMemory instance
        symbol_registry:
            SymbolRegistry instance
        embedding_pipeline:
            EmbeddingPipeline instance
        import_graph:
            Import graph from P2a-5
        embedding_cache:
            Optional embedding cache (key: query_text, value: embedding)
        """
        self.semantic_memory = semantic_memory
        self.symbol_registry = symbol_registry
        self.embedding_pipeline = embedding_pipeline
        self.import_graph = import_graph
        self.embedding_cache = embedding_cache or {}

    async def retrieve(
        self,
        query: str,
        budget: Any,
        workspace_root: str = "",
    ) -> list[Any]:
        """Run fast path retrieval (< 200ms).

        Steps:
        1. Get or cache embedding
        2. ANN search in semantic memory
        3. Symbol registry search
        4. Local graph expansion (1-hop for top 3)
        5. Merge and return

        Parameters
        ----------
        query:
            Query text
        budget:
            Context budget
        workspace_root:
            Workspace root for scoping

        Returns
        -------
        list[ContextChunk]:
            Retrieved chunks from fast path
        """
        start_time = time.perf_counter()

        chunks: list[Any] = []

        # Step 1: Get or create embedding (with cache)
        embedding = await self._get_embedding(query)
        if embedding is None:
            logger.warning("Failed to get embedding")
            return []

        # Step 2: ANN search
        ann_results = await self._ann_search(embedding)
        chunks.extend(ann_results)

        # Step 3: Symbol search
        symbol_results = await self._symbol_search(query)
        chunks.extend(symbol_results)

        # Step 4: Local graph expansion
        graph_results = await self._expand_local_graph(ann_results)
        chunks.extend(graph_results)

        # Deduplicate and rank
        chunks = self._deduplicate_chunks(chunks)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(f"Fast path complete: {len(chunks)} chunks in {elapsed_ms:.0f}ms")

        if elapsed_ms > self.TARGET_LATENCY_MS:
            logger.warning(f"Fast path exceeded target latency: {elapsed_ms:.0f}ms > {self.TARGET_LATENCY_MS}ms")

        return chunks

    async def _get_embedding(self, query: str) -> Any | None:
        """Get or create query embedding with caching.

        Parameters
        ----------
        query:
            Query text

        Returns
        -------
        Any | None:
            Embedding vector or None if failed
        """
        # Check cache
        if query in self.embedding_cache:
            logger.debug("Using cached embedding")
            return self.embedding_cache[query]["vector"]

        try:
            # Generate embedding
            embedding = await self.embedding_pipeline.embed_text(query)

            # Cache it
            self.embedding_cache[query] = {
                "vector": embedding,
                "timestamp": time.time(),
            }

            return embedding
        except Exception as e:
            logger.error(f"Failed to embed query: {e}")
            return None

    async def _ann_search(self, embedding: Any) -> list[Any]:
        """Search semantic memory with ANN.

        Parameters
        ----------
        embedding:
            Query embedding vector

        Returns
        -------
        list[ContextChunk]:
            ANN search results
        """
        try:
            # Search semantic memory
            results = await self.semantic_memory.search(
                embedding=embedding,
                limit=self.ANN_RESULTS_LIMIT,
            )

            # Convert to ContextChunk
            chunks = []
            for result in results:
                chunk = self._result_to_chunk(result, source="semantic")
                chunks.append(chunk)

            logger.debug(f"ANN search returned {len(chunks)} results")
            return chunks

        except Exception as e:
            logger.error(f"ANN search failed: {e}")
            return []

    async def _symbol_search(self, query: str) -> list[Any]:
        """Search symbol registry for key terms.

        Parameters
        ----------
        query:
            Query text

        Returns
        -------
        list[ContextChunk]:
            Symbol search results
        """
        try:
            # Extract key terms from query
            terms = self._extract_key_terms(query)

            chunks = []
            for term in terms:
                # Search symbols with LIKE pattern
                pattern = f"%{term}%"
                symbols = await self.symbol_registry.search_symbols(pattern)

                for symbol in symbols:
                    chunk = self._symbol_to_chunk(symbol, source="symbol")
                    chunks.append(chunk)

            logger.debug(f"Symbol search returned {len(chunks)} results")
            return chunks

        except Exception as e:
            logger.error(f"Symbol search failed: {e}")
            return []

    async def _expand_local_graph(self, ann_results: list[Any]) -> list[Any]:
        """Expand top ANN results via local graph (1-hop imports).

        Parameters
        ----------
        ann_results:
            Top ANN results

        Returns
        -------
        list[ContextChunk]:
            Expanded graph results
        """
        chunks = []

        try:
            # For top 3 ANN results
            for result in ann_results[: self.TOP_RESULTS_FOR_EXPANSION]:
                # Get source file from result metadata
                source_file = result.metadata.get("file_path")
                if not source_file:
                    continue

                # Get 1-hop import neighbors
                neighbors = await self.import_graph.get_neighbors(
                    node=source_file,
                    depth=self.LOCAL_GRAPH_HOP_DEPTH,
                    edge_type="imports",
                )

                # Add neighbors as chunks
                for neighbor in neighbors:
                    chunk = self._neighbor_to_chunk(neighbor, source="import_graph")
                    chunks.append(chunk)

            logger.debug(f"Local graph expansion added {len(chunks)} results")
            return chunks

        except Exception as e:
            logger.debug(f"Local graph expansion failed: {e}")
            return []

    def _extract_key_terms(self, query: str) -> list[str]:
        """Extract key terms from query for symbol search.

        Parameters
        ----------
        query:
            Query text

        Returns
        -------
        list[str]:
            Key terms (words > 3 chars)
        """
        # Remove common words
        common_words = {"what", "when", "where", "why", "how", "the", "and", "for", "with"}

        # Extract words
        words = re.findall(r"\b\w+\b", query.lower())

        # Filter: > 3 chars and not common
        terms = [w for w in words if len(w) > 3 and w not in common_words]

        return list(set(terms))[:5]  # Limit to 5 terms

    def _result_to_chunk(self, result: Any, source: str) -> Any:
        """Convert search result to ContextChunk.

        Parameters
        ----------
        result:
            Search result from semantic memory
        source:
            Source type ("semantic", "symbol", etc.)

        Returns
        -------
        ContextChunk:
            Converted chunk
        """
        from velune.retrieval.pipeline import ContextChunk

        return ContextChunk(
            id=f"{source}_{result.id}",
            source=source,
            content=result.content,
            metadata={
                "result_id": result.id,
                "tokens": len(result.content) // 4,
                "relevance": getattr(result, "relevance", 0.0),
            },
            relevance_score=getattr(result, "relevance", 0.0),
        )

    def _symbol_to_chunk(self, symbol: Any, source: str) -> Any:
        """Convert symbol to ContextChunk.

        Parameters
        ----------
        symbol:
            Symbol from registry
        source:
            Source type

        Returns
        -------
        ContextChunk:
            Converted chunk
        """
        from velune.retrieval.pipeline import ContextChunk

        content = f"{symbol.kind}: {symbol.name}\nFile: {symbol.file_path}\nLines: {symbol.line_start}-{symbol.line_end}"
        if symbol.docstring:
            content += f"\nDocstring: {symbol.docstring}"

        return ContextChunk(
            id=f"{source}_{symbol.id}",
            source=source,
            content=content,
            metadata={
                "symbol_id": symbol.id,
                "symbol_kind": symbol.kind,
                "tokens": len(content) // 4,
            },
            relevance_score=0.5,
        )

    def _neighbor_to_chunk(self, neighbor: Any, source: str) -> Any:
        """Convert graph neighbor to ContextChunk.

        Parameters
        ----------
        neighbor:
            Graph neighbor node
        source:
            Source type

        Returns
        -------
        ContextChunk:
            Converted chunk
        """
        from velune.retrieval.pipeline import ContextChunk

        return ContextChunk(
            id=f"{source}_{neighbor.get('id', neighbor.get('path'))}",
            source=source,
            content=f"Related file: {neighbor.get('path')}\nType: {neighbor.get('type')}",
            metadata={
                "node_id": neighbor.get("id"),
                "path": neighbor.get("path"),
                "tokens": 50,
            },
            relevance_score=0.4,
        )

    def _deduplicate_chunks(self, chunks: list[Any]) -> list[Any]:
        """Remove duplicate chunks keeping highest relevance score.

        Parameters
        ----------
        chunks:
            List of chunks

        Returns
        -------
        list[ContextChunk]:
            Deduplicated chunks
        """
        seen: dict[str, Any] = {}

        for chunk in chunks:
            if chunk.id not in seen or chunk.relevance_score > seen[chunk.id].relevance_score:
                seen[chunk.id] = chunk

        return list(seen.values())
