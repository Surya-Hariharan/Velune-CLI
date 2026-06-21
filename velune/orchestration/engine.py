"""Context-aware orchestration engine.

Coordinates context budget creation, retrieval, and assembly across
the orchestration pipeline. Ensures final context respects token limits.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from velune.cli.modes import SessionMode
from velune.cognition.intent import IntentClassifier, IntentType
from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection
from velune.context.token_counter import TokenCounter

if TYPE_CHECKING:
    from velune.cognition.budget import CouncilExecutionBudget
    from velune.cognition.council.factory import CouncilAgentFactory
    from velune.core.types.model import ModelDescriptor
    from velune.orchestration.schemas import OrchestrationRequest, OrchestrationResult
    from velune.retrieval.pipeline import RetrievalPipeline, RetrievedContext

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
        council_factory: CouncilAgentFactory | None = None,
    ) -> None:
        """Initialize orchestration engine.

        Args:
            retrieval_pipeline: Optional RetrievalPipeline instance for context retrieval
            council_factory: Optional CouncilAgentFactory for multi-model role dispatch.
                             When present, :meth:`execute` routes through CouncilRunner.
        """
        self.retrieval_pipeline = retrieval_pipeline
        self._council_factory = council_factory
        self.assembler = ContextAssembler()
        self._intent_classifier = IntentClassifier()

    def classify_intent(self, prompt: str, existing_intent: str | None = None) -> IntentType:
        """Classify *prompt* into an ``IntentType``.

        If *existing_intent* is already set on the request (e.g., the caller
        pre-classified it), validate and return it; otherwise classify fresh.
        """
        if existing_intent:
            try:
                return IntentType(existing_intent)
            except ValueError:
                pass
        intent, confidence = self._intent_classifier.classify_with_confidence(prompt)
        logger.debug(
            "Classified intent=%s (confidence=%.2f) for prompt: %.60s", intent, confidence, prompt
        )
        return intent

    async def execute(
        self,
        request: OrchestrationRequest,
        context: str = "",
        budget: CouncilExecutionBudget | None = None,
    ) -> OrchestrationResult:
        """Execute a full orchestration request through the council pipeline.

        Classifies the intent, then delegates to :class:`CouncilRunner` if a
        council factory is configured.  Falls back to a lightweight single-pass
        response when no factory is available (e.g. in tests or CLI ask mode).

        Args:
            request: Typed orchestration request.
            context: Pre-assembled context string.
            budget: Optional council execution budget override.

        Returns:
            :class:`OrchestrationResult` with ``success``, ``output``, and metadata.
        """
        from velune.orchestration.schemas import ExecutionStatus, OrchestrationResult

        intent = self.classify_intent(request.prompt, existing_intent=request.intent)
        logger.info("execute(): run_id=%s intent=%s", request.task_id or "(new)", intent.value)

        if self._council_factory is not None:
            from velune.cognition.council_runner import CouncilRunner

            runner = CouncilRunner(
                factory=self._council_factory,
                default_budget=budget,
            )
            return await runner.run(request, context=context, budget=budget)

        # No council factory — lightweight fallback (no LLM calls)
        logger.warning(
            "execute(): no council_factory configured; returning empty result for run_id=%s",
            request.task_id,
        )
        return OrchestrationResult(
            run_id=request.task_id or "no-council",
            task_id=request.task_id or "no-council",
            success=False,
            status=ExecutionStatus.FAILED,
            error="No CouncilAgentFactory configured on ContextOrchestrationEngine.",
            metadata={"intent": intent.value},
        )

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

    async def orchestrate_context_retrieval(
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

        This is a coroutine and must be awaited by its callers; it participates
        in the single shared event loop owned by ``velune.kernel.entrypoint``
        rather than managing an event loop of its own.

        Args:
            query: Retrieval query string
            mode: SessionMode for budget allocation
            model: ModelDescriptor for token counting
            task_profile: Optional task profile for retrieval strategy
            workspace_root: Workspace root for scoped retrieval

        Returns:
            Tuple of (context_chunks, budget)
        """
        # Phase 0: Classify intent (zero-latency, no LLM call)
        intent = self.classify_intent(query)

        # Phase 1: Create budget for this session
        budget = self.create_budget(mode, model)
        logger.debug("Budget allocation: %s | intent: %s", budget, intent.value)

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

        # Retrieve context within the caller's event loop. No manual loop
        # management: the orchestration engine is async end-to-end and the
        # single event loop is owned by velune.kernel.entrypoint.run_async.
        retrieved = await self.retrieval_pipeline.retrieve(
            query=query,
            budget=budget,
            task_profile=task_profile,
            workspace_root=workspace_root,
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
