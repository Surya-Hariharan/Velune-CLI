"""Semantic Context Compressor."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from velune.context.window import estimate_tokens
from velune.providers.base import ModelProvider

if TYPE_CHECKING:
    from velune.telemetry.cognition import CognitivePerformanceAnalytics

logger = logging.getLogger("velune.context.compressor")


class ContextCompressor:
    """Uses LLM-guided semantic compression to distill large logs or files to fit context constraints."""

    def __init__(self, analytics: CognitivePerformanceAnalytics | None = None) -> None:
        from velune.telemetry.cognition import CognitivePerformanceAnalytics
        self.analytics = analytics or CognitivePerformanceAnalytics()



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
        Enforces 'Cognitive Continuity Guardrails' to preserve incomplete steps and errors.
        """
        # Cognitive Continuity Guardrails: Extract critical lines that must NOT be compressed
        critical_lines = []
        regular_lines = []

        for line in content.splitlines():
            stripped = line.strip()
            is_critical = (
                stripped.startswith("[ ]") or
                stripped.startswith("- [ ]") or
                stripped.startswith("[/") or
                stripped.startswith("- [/]") or
                "ERROR" in stripped.upper() or
                "CRITICAL" in stripped.upper() or
                "FAIL" in stripped.upper() or
                "GUIDELINE" in stripped.upper() or
                "ARCHITECTURAL" in stripped.upper()
            )
            if is_critical:
                critical_lines.append(line)
            else:
                regular_lines.append(line)

        critical_text = "\n".join(critical_lines)
        critical_tokens = estimate_tokens(critical_text) if critical_lines else 0

        # If regular_lines is empty, just return the critical content
        if not regular_lines:
            return content

        # Compute remaining budget for the non-critical content
        remaining_budget = max(100, target_token_budget - critical_tokens)
        content_to_compress = "\n".join(regular_lines)

        current_tokens = estimate_tokens(content_to_compress)
        if current_tokens <= remaining_budget:
            # If everything fits under the budget, no need to compress
            return content

        logger.info(
            "Compressing regular content: %d -> budget %d. Preserving %d critical lines (%d tokens).",
            current_tokens, remaining_budget, len(critical_lines), critical_tokens
        )

        prompt = (
            f"You are a context compression engine. Your goal is to compress the following content "
            f"so that it fits within a budget of {remaining_budget} tokens.\n\n"
            f"RULES:\n"
            f"1. Retain all critical variable names, function signatures, dependencies, and core intent.\n"
            f"2. Remove fluff, boilerplate, verbose explanations, and redundant comments.\n"
            f"3. Output ONLY the compressed representation without any introduction or markdown wrappers.\n\n"
            f"Content to compress:\n"
            f"{content_to_compress}"
        )

        compressed = None
        method = "llm"
        start_time = time.time()

        if provider is not None:
            try:
                from velune.core.types.inference import InferenceRequest
                infer_request = InferenceRequest(
                    model_id=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,  # Very low temperature for deterministic compression
                    max_tokens=target_token_budget * 2,  # Allow headroom
                )
                response = await asyncio.wait_for(
                    provider.infer(infer_request),
                    timeout=15.0  # Don't block forever
                )
                compressed = response.content.strip()
                logger.info("LLM compression successful: %d → %d tokens",
                            current_tokens, estimate_tokens(compressed))
            except (TimeoutError, Exception) as e:
                logger.warning("LLM compression failed (%s), using extractive fallback", type(e).__name__)

        if compressed is None:
            method = "extractive"
            # Deterministic extractive fallback
            from velune.context.extractive import extractive_compress
            compressed = extractive_compress(content_to_compress, remaining_budget)
            logger.info("Extractive compression successful: %d → %d tokens",
                        current_tokens, estimate_tokens(compressed))

        latency_ms = int((time.time() - start_time) * 1000)
        self.analytics.record_compression(
            original_tokens=current_tokens,
            compressed_tokens=estimate_tokens(compressed),
            method=method,
            latency_ms=latency_ms,
        )

        # Reconstruct with critical lines preserved at the top
        final_content = []
        if critical_lines:
            final_content.append("### CRITICAL SYSTEM GUIDELINES & UNRESOLVED STEPS (PRESERVED) ###")
            final_content.append(critical_text)
            final_content.append("### COMPRESSED CONTEXT ###")
        final_content.append(compressed)

        reconstructed = "\n".join(final_content)
        logger.info("Compressed context successfully with guardrails using %s.", method)
        return reconstructed

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
