"""Semantic Context Compressor."""

from __future__ import annotations

import logging
from typing import Any
from velune.providers.base import ModelProvider
from velune.context.window import estimate_tokens

logger = logging.getLogger("velune.context.compressor")


class ContextCompressor:
    """Uses LLM-guided semantic compression to distill large logs or files to fit context constraints."""

    def __init__(self) -> None:
        pass

    async def compress(
        self,
        content: str,
        provider: ModelProvider,
        model_id: str,
        target_token_budget: int = 1000,
    ) -> str:
        """
        Uses an LLM (Synthesizer/Reasoner) to compress large blocks of text,
        such as docstrings or histories, into dense factual representations.
        """
        current_tokens = estimate_tokens(content)
        if current_tokens <= target_token_budget:
            return content

        logger.info("Content size %d exceeds budget %d. Compressing...", current_tokens, target_token_budget)

        prompt = (
            f"You are a context compression engine. Your goal is to compress the following content "
            f"so that it fits within a budget of {target_token_budget} tokens.\n\n"
            f"RULES:\n"
            f"1. Retain all critical variable names, function signatures, dependencies, and core intent.\n"
            f"2. Remove fluff, boilerplate, verbose explanations, and redundant comments.\n"
            f"3. Output ONLY the compressed representation without any introduction or markdown wrappers.\n\n"
            f"Content to compress:\n"
            f"{content}"
        )

        try:
            response = await provider.complete(prompt=prompt, model=model_id)
            compressed = response.text.strip()
            
            new_tokens = estimate_tokens(compressed)
            logger.info("Compressed context successfully: %d -> %d tokens.", current_tokens, new_tokens)
            return compressed
        except Exception as e:
            logger.error("Failed to semantically compress context: %s. Returning raw truncated.", e)
            # Safe truncation fallback
            chars_limit = target_token_budget * 4
            return content[:chars_limit] + "\n... [TRUNCATED] ..."
class ContextBudgetManager:
    """Manages the token budget allocation for context assembly."""

    def __init__(self, max_tokens: int = 8192) -> None:
        self.max_tokens = max_tokens
        # Budget allocations (ratios)
        self.working_ratio = 0.20      # Chronological conversation turns
        self.episodic_ratio = 0.20     # SQLite execution logs / steps
        self.semantic_ratio = 0.30     # Relevant code vectors / facts
        self.repository_ratio = 0.30   # AST structures / git diffs

    def allocate(self) -> dict[str, int]:
        """Convert allocation ratios to precise token counts."""
        return {
            "working": int(self.max_tokens * self.working_ratio),
            "episodic": int(self.max_tokens * self.episodic_ratio),
            "semantic": int(self.max_tokens * self.semantic_ratio),
            "repository": int(self.max_tokens * self.repository_ratio),
        }
