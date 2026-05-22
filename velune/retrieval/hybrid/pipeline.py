"""Hybrid retrieval orchestration."""

from typing import list, Dict, Optional
from velune.retrieval.vector.store import VectorStore
from velune.retrieval.lexical.searcher import BM25Searcher
from velune.retrieval.graph.retriever import GraphRetriever
from velune.retrieval.hybrid.fusion import ReciprocalRankFusion
from velune.retrieval.reranker.cross_encoder import CrossEncoderReranker


class HybridRetrievalPipeline:
    """Pipeline for hybrid retrieval combining vector, lexical, and graph."""

    def __init__(
        self,
        vector_store: VectorStore,
        lexical_searcher: BM25Searcher,
        graph_retriever: GraphRetriever,
    ):
        self.vector_store = vector_store
        self.lexical_searcher = lexical_searcher
        self.graph_retriever = graph_retriever
        self.fusion = ReciprocalRankFusion()
        self.reranker = CrossEncoderReranker()

    async def retrieve(
        self,
        query: str,
        query_embedding: list[float],
        limit: int = 10,
        use_vector: bool = True,
        use_lexical: bool = True,
        use_graph: bool = False,
        entity_id: Optional[str] = None,
        rerank: bool = True,
    ) -> list[Dict[str, any]]:
        """Retrieve using hybrid approach."""
        result_lists = []
        
        # Vector retrieval
        if use_vector:
            vector_results = await self.vector_store.query(query_embedding, n_results=limit)
            formatted_vector = self._format_vector_results(vector_results)
            result_lists.append(formatted_vector)
        
        # Lexical retrieval
        if use_lexical:
            lexical_results = self.lexical_searcher.search(query, limit=limit)
            result_lists.append(lexical_results)
        
        # Graph retrieval
        if use_graph and entity_id:
            graph_results = self.graph_retriever.retrieve(entity_id, limit=limit)
            result_lists.append(graph_results)
        
        # Fuse results
        if not result_lists:
            return []
        
        fused = self.fusion.fuse(result_lists, limit=limit * 2)
        
        # Rerank
        if rerank:
            fused = await self.reranker.rerank(query, fused, limit=limit)
        
        return fused[:limit]

    def _format_vector_results(self, vector_results: Dict[str, any]) -> list[Dict[str, any]]:
        """Format vector store results."""
        formatted = []
        if not vector_results.get("ids"):
            return formatted
        
        for i, doc_id in enumerate(vector_results["ids"][0]):
            formatted.append({
                "id": doc_id,
                "score": vector_results["distances"][0][i] if vector_results.get("distances") else 0.0,
                "retrieval_method": "vector",
            })
        
        return formatted
