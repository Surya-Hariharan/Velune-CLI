import logging

from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule

logger = logging.getLogger("velune.retrieval.module")


def _create_hybrid_retriever(env: RuntimeEnvironment):
    from velune.core.paths import qdrant_store_path
    from velune.retrieval.hybrid import HybridRetriever, load_lexical_documents

    vector_path = str(qdrant_store_path(env.workspace))
    code_vector_client = env.container.get("runtime.code_vector_client")
    retriever = HybridRetriever(
        location=vector_path,
        client_provider=lambda: code_vector_client.client,
    )

    # Populate BM25 from the persisted retrieval index written by
    # cognition._save_retrieval_index, and bind the path so the retriever can
    # re-hydrate itself when a later re-index rewrites that file.
    retrieval_index_path = env.workspace / ".velune" / "retrieval_index.json"
    try:
        docs = load_lexical_documents(retrieval_index_path)
        if docs:
            retriever.lexical_retriever.add_documents_batch(docs)
            logger.info(
                "BM25 index loaded: %d workspace documents from %s",
                len(docs),
                retrieval_index_path,
            )
        retriever.bind_lexical_index(retrieval_index_path, loaded=bool(docs))
    except Exception as exc:
        logger.debug("Could not load retrieval index: %s", exc)
        retriever.bind_lexical_index(retrieval_index_path, loaded=False)

    return retriever


def _create_retrieval_planner(env: RuntimeEnvironment):
    from velune.retrieval.planner import RetrievalPlanner

    retrieval_config = getattr(env.config, "retrieval", None)
    return RetrievalPlanner(config=retrieval_config)


def _create_retrieval_feedback_recorder(env: RuntimeEnvironment):
    from velune.retrieval.feedback import RetrievalFeedbackRecorder

    return RetrievalFeedbackRecorder()


RETRIEVAL_MODULES = [
    SubsystemModule(
        name="retrieval",
        factory=_create_hybrid_retriever,
        container_key="runtime.retrieval",
        lifecycle_key="retrieval",
        dependencies=["runtime.code_vector_client"],
    ),
    # No lifecycle_key: a plain, stateful-but-async-free object (an
    # intent->weights lookup table plus a small result cache) — nothing to
    # start or stop. Kept in the container (rather than constructed fresh
    # per call) specifically so its result cache persists across turns in a
    # session instead of starting empty every time.
    SubsystemModule(
        name="retrieval_planner",
        factory=_create_retrieval_planner,
        container_key="runtime.retrieval_planner",
    ),
    # Same reasoning: a plain object accumulating a bounded in-memory
    # history across the session — must be a shared instance, not
    # reconstructed (and emptied) on every turn.
    SubsystemModule(
        name="retrieval_feedback",
        factory=_create_retrieval_feedback_recorder,
        container_key="runtime.retrieval_feedback",
    ),
]
