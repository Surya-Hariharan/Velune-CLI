"""Slow path retriever: deep graph traversal (< 2000ms, HIGH complexity only)."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("velune.retrieval.slow_path")


class SlowPathRetriever:
    """Slow path using deep graph traversal and contextual search."""

    # Traversal limits
    CALLS_DEPTH = 2
    IMPORTS_DEPTH = 1
    TARGET_LATENCY_MS = 2000

    def __init__(
        self,
        fast_path_retriever: Any,
        import_graph: Any,
        call_graph: Any,
        episodic_memory: Any,
        lineage_memory: Any,
    ) -> None:
        """Initialize slow path retriever.

        Parameters
        ----------
        fast_path_retriever:
            FastPathRetriever instance (to reuse results)
        import_graph:
            Import graph for traversal
        call_graph:
            Call graph for traversal
        episodic_memory:
            Episodic memory for context
        lineage_memory:
            Lineage memory for decisions/failures
        """
        self.fast_path = fast_path_retriever
        self.import_graph = import_graph
        self.call_graph = call_graph
        self.episodic_memory = episodic_memory
        self.lineage_memory = lineage_memory

    async def retrieve(
        self,
        query: str,
        fast_results: list[Any],
        budget: Any,
        workspace_root: str = "",
    ) -> list[Any]:
        """Run slow path retrieval (< 2000ms).

        Steps:
        1. Identify high-blast-radius nodes from fast path
        2. Traverse CALLS edges (depth 2)
        3. Traverse IMPORTS edges (depth 1)
        4. Search episodic memory for sessions mentioning these nodes
        5. Retrieve lineage decisions/failures
        6. Return new chunks not in fast path

        Parameters
        ----------
        query:
            Query text (for logging/context)
        fast_results:
            Results from fast path (starting points)
        budget:
            Context budget
        workspace_root:
            Workspace root for scoping

        Returns
        -------
        list[ContextChunk]:
            Additional chunks from slow path
        """
        start_time = time.perf_counter()

        chunks: list[Any] = []

        # Step 1: Identify high-blast-radius nodes
        blast_radius_nodes = self._identify_high_blast_radius_nodes(fast_results)
        logger.debug(f"Identified {len(blast_radius_nodes)} high-blast-radius nodes")

        # Step 2: Traverse CALLS edges
        call_graph_results = await self._traverse_call_graph(blast_radius_nodes)
        chunks.extend(call_graph_results)

        # Step 3: Traverse IMPORTS edges
        import_graph_results = await self._traverse_import_graph(blast_radius_nodes)
        chunks.extend(import_graph_results)

        # Step 4: Search episodic memory
        episodic_results = await self._search_episodic_context(blast_radius_nodes, workspace_root)
        chunks.extend(episodic_results)

        # Step 5: Retrieve lineage decisions
        lineage_results = await self._retrieve_lineage_context(blast_radius_nodes)
        chunks.extend(lineage_results)

        # Filter: keep only chunks not already in fast_results
        fast_ids = {r.id for r in fast_results}
        filtered_chunks = [c for c in chunks if c.id not in fast_ids]

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(f"Slow path complete: {len(filtered_chunks)} new chunks in {elapsed_ms:.0f}ms")

        if elapsed_ms > self.TARGET_LATENCY_MS:
            logger.warning(
                f"Slow path exceeded target latency: {elapsed_ms:.0f}ms > {self.TARGET_LATENCY_MS}ms"
            )

        return filtered_chunks

    def _identify_high_blast_radius_nodes(self, fast_results: list[Any]) -> list[str]:
        """Identify high-blast-radius nodes from fast path results.

        High-blast-radius: files/symbols that many other nodes depend on.

        Parameters
        ----------
        fast_results:
            Fast path results

        Returns
        -------
        list[str]:
            Node IDs to traverse from
        """
        nodes = []

        for result in fast_results:
            # Extract node ID from metadata
            node_id = result.metadata.get("symbol_id") or result.metadata.get("path")
            if node_id:
                nodes.append(node_id)

        return nodes[:10]  # Limit to top 10

    async def _traverse_call_graph(self, start_nodes: list[str]) -> list[Any]:
        """Traverse call graph from starting nodes.

        Parameters
        ----------
        start_nodes:
            Starting node IDs

        Returns
        -------
        list[ContextChunk]:
            Chunks from call graph traversal
        """
        from velune.retrieval.pipeline import ContextChunk

        chunks = []

        try:
            # Traverse from each node
            for node_id in start_nodes:
                # Get callers (who calls this function)
                callers = await self.call_graph.get_neighbors(
                    node=node_id,
                    depth=self.CALLS_DEPTH,
                    direction="incoming",  # Who calls this
                )

                # Add as chunks
                for caller in callers:
                    chunk = ContextChunk(
                        id=f"callgraph_{caller.get('id')}",
                        source="call_graph",
                        content=f"Function: {caller.get('name')}\nCalls: {node_id}",
                        metadata={
                            "node_id": caller.get("id"),
                            "tokens": 50,
                        },
                        relevance_score=0.3,
                    )
                    chunks.append(chunk)

            logger.debug(f"Call graph traversal found {len(chunks)} results")
            return chunks

        except Exception as e:
            logger.debug(f"Call graph traversal failed: {e}")
            return []

    async def _traverse_import_graph(self, start_nodes: list[str]) -> list[Any]:
        """Traverse import graph from starting nodes.

        Parameters
        ----------
        start_nodes:
            Starting node IDs (file paths)

        Returns
        -------
        list[ContextChunk]:
            Chunks from import graph traversal
        """
        from velune.retrieval.pipeline import ContextChunk

        chunks = []

        try:
            # Traverse from each node
            for node_id in start_nodes:
                # Get import neighbors
                neighbors = await self.import_graph.get_neighbors(
                    node=node_id,
                    depth=self.IMPORTS_DEPTH,
                    edge_type="imports",
                )

                # Add as chunks
                for neighbor in neighbors:
                    chunk = ContextChunk(
                        id=f"imports_{neighbor.get('id')}",
                        source="import_graph",
                        content=f"Module: {neighbor.get('path')}\nImports: {node_id}",
                        metadata={
                            "node_id": neighbor.get("id"),
                            "path": neighbor.get("path"),
                            "tokens": 50,
                        },
                        relevance_score=0.25,
                    )
                    chunks.append(chunk)

            logger.debug(f"Import graph traversal found {len(chunks)} results")
            return chunks

        except Exception as e:
            logger.debug(f"Import graph traversal failed: {e}")
            return []

    async def _search_episodic_context(
        self,
        node_ids: list[str],
        workspace_root: str,
    ) -> list[Any]:
        """Search episodic memory for sessions mentioning these nodes.

        Parameters
        ----------
        node_ids:
            Node IDs to search for
        workspace_root:
            Workspace root for scoping

        Returns
        -------
        list[ContextChunk]:
            Chunks from episodic memory
        """
        from velune.retrieval.pipeline import ContextChunk

        chunks = []

        try:
            # Search for sessions mentioning these nodes
            for node_id in node_ids:
                # Convert node ID to searchable form
                search_term = node_id.split("/")[-1]  # Get filename

                # LIKE search in episodic memory
                results = await self.episodic_memory.search_turns(
                    pattern=f"%{search_term}%",
                    limit=3,
                )

                # Add as chunks
                for result in results:
                    chunk = ContextChunk(
                        id=f"episodic_{result.get('turn_id')}",
                        source="episodic",
                        content=result.get("content", ""),
                        metadata={
                            "turn_id": result.get("turn_id"),
                            "session_id": result.get("session_id"),
                            "tokens": len(result.get("content", "")) // 4,
                        },
                        relevance_score=0.35,
                    )
                    chunks.append(chunk)

            logger.debug(f"Episodic context search found {len(chunks)} results")
            return chunks

        except Exception as e:
            logger.debug(f"Episodic context search failed: {e}")
            return []

    async def _retrieve_lineage_context(self, node_ids: list[str]) -> list[Any]:
        """Retrieve lineage decisions/failures related to these nodes.

        Parameters
        ----------
        node_ids:
            Node IDs to search for

        Returns
        -------
        list[ContextChunk]:
            Chunks from lineage memory
        """
        from velune.retrieval.pipeline import ContextChunk

        chunks = []

        try:
            # Search lineage for decisions/failures
            for node_id in node_ids:
                # Get related decisions
                decisions = await self.lineage_memory.get_decisions(
                    node_id=node_id,
                    limit=2,
                )

                for decision in decisions:
                    chunk = ContextChunk(
                        id=f"lineage_decision_{decision.get('id')}",
                        source="lineage",
                        content=f"Decision: {decision.get('summary')}\nReason: {decision.get('reasoning')}",
                        metadata={
                            "decision_id": decision.get("id"),
                            "node_id": node_id,
                            "tokens": len(decision.get("reasoning", "")) // 4,
                        },
                        relevance_score=0.45,
                    )
                    chunks.append(chunk)

                # Get related failures
                failures = await self.lineage_memory.get_failures(
                    node_id=node_id,
                    limit=2,
                )

                for failure in failures:
                    chunk = ContextChunk(
                        id=f"lineage_failure_{failure.get('id')}",
                        source="lineage",
                        content=f"Failure: {failure.get('summary')}\nMitigation: {failure.get('mitigation')}",
                        metadata={
                            "failure_id": failure.get("id"),
                            "node_id": node_id,
                            "tokens": len(failure.get("mitigation", "")) // 4,
                        },
                        relevance_score=0.4,
                    )
                    chunks.append(chunk)

            logger.debug(f"Lineage context retrieval found {len(chunks)} results")
            return chunks

        except Exception as e:
            logger.debug(f"Lineage context retrieval failed: {e}")
            return []
