"""Context-aware orchestration engine.

Coordinates context budget creation, retrieval, and assembly across
the orchestration pipeline. Ensures final context respects token limits.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from velune.cli.modes import SessionMode
from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection
from velune.context.token_counter import TokenCounter

if TYPE_CHECKING:
    from velune.core.types.model import ModelDescriptor
    from velune.retrieval.pipeline import RetrievedContext, RetrievalPipeline

logger = logging.getLogger("velune.orchestration.engine")


class ContextOrchestrationEngine:
    """Orchestrates context budget allocation and assembly.

    Responsible for:
    1. Creating ContextBudget based on SessionMode and model
    2. Coordinating retrieval within budget constraints
    3. Assembling context chunks into final context string
    4. Verifying final context fits within budget
    """

    def __init__(
        self,
        retrieval_pipeline: RetrievalPipeline | None = None,
    ) -> None:
        """Initialize orchestration engine.

        Args:
            retrieval_pipeline: Optional RetrievalPipeline instance for context retrieval
        """
        self.retrieval_pipeline = retrieval_pipeline
        self.assembler = ContextAssembler()

    def create_budget(
        self,
        mode: SessionMode,
        model: ModelDescriptor,
    ) -> ContextBudget:
        """Create context budget for the given mode and model.

        Args:
            mode: SessionMode (OPTIMUS, NORMAL, or GODLY)
            model: ModelDescriptor with context_length information

        Returns:
            ContextBudget configured for the mode
        """
        budget = ContextBudget.from_mode(mode, model.context_length)
        logger.debug(f"Created {budget} for mode={mode.value}")
        return budget

    def assemble_context(
        self,
        chunks: list[ContextChunk],
        budget: ContextBudget,
        model: ModelDescriptor,
    ) -> tuple[str, int]:
        """Assemble context chunks into final context respecting budget.

        Args:
            chunks: List of ContextChunk objects to assemble
            budget: ContextBudget constraints
            model: ModelDescriptor for token counting

        Returns:
            Tuple of (assembled_context_string, final_token_count)

        Raises:
            ValueError: If critical sections cannot fit in budget
        """
        assembled_context, report = self.assembler.assemble(
            chunks=chunks,
            budget=budget,
            model=model,
        )

        logger.debug(f"Assembly report: {report}")

        final_token_count = TokenCounter.count(assembled_context, model)

        if report.budget_exceeded:
            logger.warning(
                f"Context assembly budget exceeded: "
                f"{final_token_count} tokens > {budget.usable_tokens} allocated; "
                f"dropped lowest-priority sections"
            )

        if final_token_count > budget.total_tokens:
            logger.error(
                f"CRITICAL: Final context ({final_token_count} tokens) "
                f"exceeds total budget ({budget.total_tokens}); "
                f"system and output sections may be violated"
            )

        return assembled_context, final_token_count

    def orchestrate_context_retrieval(
        self,
        query: str,
        mode: SessionMode,
        model: ModelDescriptor,
        task_profile: Any | None = None,
        workspace_root: str = "",
    ) -> tuple[list[ContextChunk], ContextBudget]:
        """Orchestrate full context retrieval and budget allocation.

        Coordinates:
        1. Budget creation for mode/model
        2. Retrieval with budget constraints
        3. Conversion of retrieval results to context chunks
        4. Assembly verification

        Args:
            query: Retrieval query string
            mode: SessionMode for budget allocation
            model: ModelDescriptor for token counting
            task_profile: Optional task profile for retrieval strategy
            workspace_root: Workspace root for scoped retrieval

        Returns:
            Tuple of (context_chunks, budget)
        """
        # Phase 1: Create budget for this session
        budget = self.create_budget(mode, model)
        logger.debug(f"Budget allocation: {budget}")

        # Phase 2: Retrieve context within budget
        if self.retrieval_pipeline is None:
            logger.warning("No retrieval pipeline available; returning empty context")
            return [], budget

        # Create task profile if not provided
        if task_profile is None:
            from velune.retrieval.pipeline import TaskProfile

            task_profile = TaskProfile(
                task_type="GENERAL",
                complexity="MEDIUM",
                requires_long_context=mode == SessionMode.GODLY,
                latency_sensitive=mode == SessionMode.OPTIMUS,
            )

        # Retrieve context asynchronously
        # Note: This is a synchronous wrapper; in async contexts, call directly
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        retrieved = loop.run_until_complete(
            self.retrieval_pipeline.retrieve(
                query=query,
                budget=budget,
                task_profile=task_profile,
                workspace_root=workspace_root,
            )
        )

        # Phase 3: Convert retrieval results to context chunks
        chunks = self._convert_retrieval_results(retrieved, budget)
        logger.debug(f"Converted {len(chunks)} retrieval results to context chunks")

        return chunks, budget

    def _convert_retrieval_results(
        self,
        retrieved: RetrievedContext,
        budget: ContextBudget,
    ) -> list[ContextChunk]:
        """Convert RetrievalPipeline results to context chunks.

        Maps retrieval chunks to ContextChunk objects with appropriate
        sections, trust scores, and metadata for assembly.

        Args:
            retrieved: RetrievedContext from retrieval pipeline
            budget: ContextBudget (for reference)

        Returns:
            List of ContextChunk objects ready for assembly
        """
        chunks: list[ContextChunk] = []

        for retrieval_chunk in retrieved.chunks:
            # Map retrieval source to context section
            source = retrieval_chunk.source.lower()
            if "semantic" in source or "vector" in source:
                section = ContextSection.RETRIEVED_CONTEXT
            elif "symbol" in source:
                section = ContextSection.REPOSITORY_SNAPSHOT
            elif "episodic" in source or "memory" in source:
                section = ContextSection.WORKING_MEMORY
            elif "lineage" in source:
                section = ContextSection.COGNITIVE_CONTINUITY
            else:
                section = ContextSection.RETRIEVED_CONTEXT

            # Convert trust score (combined_score is 0-1)
            trust_score = retrieval_chunk.combined_score

            # Create context chunk
            chunk = ContextChunk(
                section=section,
                content=retrieval_chunk.content,
                token_count=self._estimate_chunk_tokens(retrieval_chunk.content),
                source=retrieval_chunk.source,
                trust_score=trust_score,
                priority=retrieval_chunk.combined_score,
                metadata={
                    "retrieval_id": retrieval_chunk.id,
                    "relevance_score": retrieval_chunk.relevance_score,
                    "recency_score": retrieval_chunk.recency_score,
                },
            )
            chunks.append(chunk)

        return chunks

    @staticmethod
    def _estimate_chunk_tokens(content: str) -> int:
        """Quick estimate of chunk token count.

        Uses conservative estimate: 1 token ≈ 1.35 words.
        """
        if not content:
            return 0
        word_count = len(content.split())
        return max(1, int(word_count * 1.35))
