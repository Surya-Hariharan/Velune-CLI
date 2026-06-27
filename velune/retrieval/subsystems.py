import json
import logging

from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule

logger = logging.getLogger("velune.retrieval.module")


def _create_hybrid_retriever(env: RuntimeEnvironment):
    from velune.core.paths import qdrant_store_path
    from velune.retrieval.hybrid import HybridRetriever
    from velune.retrieval.schemas import RetrievalDocument

    vector_path = str(qdrant_store_path(env.workspace))
    semantic_tier = env.container.get("runtime.semantic_memory")
    retriever = HybridRetriever(
        location=vector_path,
        client_provider=lambda: semantic_tier.client,
    )

    # Populate BM25 from the persisted retrieval index written by cognition._save_retrieval_index
    retrieval_index_path = env.workspace / ".velune" / "retrieval_index.json"
    if retrieval_index_path.exists():
        try:
            with open(retrieval_index_path, encoding="utf-8") as fh:
                raw_docs = json.load(fh)

            docs = [
                RetrievalDocument(
                    id=d["id"],
                    content=d["content"],
                    metadata=d.get("metadata", {}),
                    namespace="workspace",
                )
                for d in raw_docs
                if d.get("id") and d.get("content")
            ]
            if docs:
                retriever.lexical_retriever.add_documents_batch(docs)
                logger.info(
                    "BM25 index loaded: %d workspace documents from %s",
                    len(docs),
                    retrieval_index_path,
                )
        except Exception as exc:
            logger.debug("Could not load retrieval index: %s", exc)

    return retriever


RETRIEVAL_MODULES = [
    SubsystemModule(
        name="retrieval",
        factory=_create_hybrid_retriever,
        container_key="runtime.retrieval",
        lifecycle_key="retrieval",
        dependencies=["runtime.semantic_memory"],
    )
]
