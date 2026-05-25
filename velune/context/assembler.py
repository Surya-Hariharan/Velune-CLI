"""Budgeted Context Assembler.

Main coordinator of the Context Engineering subsystem. Manages budget allocations,
scores, compresses, and stitches final LLM system prompts.
"""

from __future__ import annotations

import logging
from typing import Any

from velune.context.compressor import ContextBudgetManager, ContextCompressor
from velune.context.scorer import ContextScorer
from velune.context.stitcher import ContextStitcher
from velune.context.window import ContextWindowTracker, estimate_tokens
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.context.assembler")


class ContextAssembler:
    """Orchestrates dynamic multi-source retrieval, token budgeting, and semantic prompt construction."""

    def __init__(
        self,
        max_tokens: int = 8192,
        scorer: ContextScorer | None = None,
        compressor: ContextCompressor | None = None,
        stitcher: ContextStitcher | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.scorer = scorer or ContextScorer()
        self.compressor = compressor or ContextCompressor()
        self.stitcher = stitcher or ContextStitcher()
        self.budget_manager = ContextBudgetManager(max_tokens=max_tokens)
        self.tracker = ContextWindowTracker(max_tokens=max_tokens)

    async def assemble(
        self,
        working_turns: list[dict[str, Any]],
        episodic_steps: list[dict[str, Any]],
        semantic_chunks: list[dict[str, Any]],
        provider: ModelProvider,
        model_id: str,
        repository_ast: str | None = None,
        git_diffs: str | None = None,
    ) -> str:
        """
        Assemble, score, compress, and stitch context elements into a single formatted prompt.
        """
        budgets = self.budget_manager.allocate()
        logger.info("Assembling context under budgets: %s", budgets)

        # 1. Compress working conversation turns if exceeding budget
        working_text = "\n".join([f"{t.get('role', 'user')}: {t.get('content', '')}" for t in working_turns])
        if estimate_tokens(working_text) > budgets["working"]:
            logger.info("Working memory turns exceed budget. Compressing...")
            compressed_turns_text = await self.compressor.compress(
                content=working_text,
                provider=provider,
                model_id=model_id,
                target_token_budget=budgets["working"],
            )
            # Re-package as a single turn for prompt stitching
            working_turns = [{"role": "system", "content": f"Compressed conversation history:\n{compressed_turns_text}"}]

        # 2. Score and rank semantic chunks, keeping only what fits in the semantic budget
        ranked_chunks = self.scorer.rank_items(semantic_chunks)
        selected_chunks = []
        accumulated_tokens = 0

        for chunk in ranked_chunks:
            chunk_tokens = estimate_tokens(str(chunk))
            if accumulated_tokens + chunk_tokens <= budgets["semantic"]:
                selected_chunks.append(chunk)
                accumulated_tokens += chunk_tokens
            else:
                break

        # 3. Compress repository AST structure if it exceeds budget
        if repository_ast and estimate_tokens(repository_ast) > budgets["repository"]:
            logger.info("Repository AST structure exceeds budget. Compressing...")
            repository_ast = await self.compressor.compress(
                content=repository_ast,
                provider=provider,
                model_id=model_id,
                target_token_budget=budgets["repository"],
            )

        # 4. Stitch everything together
        final_prompt = self.stitcher.stitch(
            working_turns=working_turns,
            episodic_steps=episodic_steps,
            semantic_chunks=selected_chunks,
            repository_ast=repository_ast,
            git_diffs=git_diffs,
        )

        return final_prompt
