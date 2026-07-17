"""Context compaction pipeline for summarizing long sessions.

Compresses long conversation histories into structured summaries,
preserving key facts while dramatically reducing token count.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("velune.memory.compaction")


@dataclass
class CompactionStats:
    """Statistics from a compaction operation."""

    original_turn_count: int
    original_token_count: int
    summary_token_count: int
    compression_ratio: float  # original / summary
    turns_compacted: int
    turns_kept: int
    timestamp: float


class ContextCompactor:
    """Compresses long conversation histories into summaries."""

    # Configuration
    MIN_TURNS_FOR_COMPACTION = 30
    KEEP_LAST_N_TURNS = 10
    CONTEXT_UTILIZATION_THRESHOLD = 0.75
    MIN_SUMMARY_LENGTH = 100
    MAX_SUMMARY_SIZE_RATIO = 0.30  # Summary must be <= 30% of original
    COMPRESSION_RATIO_TARGET = 5.0  # Aim for at least 5:1 compression

    def __init__(
        self,
        provider: Any,  # InferenceProvider for summarization
        working_tier: Any,
        episodic_memory: Any,
        max_context_tokens: int = 100000,
    ) -> None:
        """Initialize compactor.

        Parameters
        ----------
        provider:
            LLM provider for summarization
        working_tier:
            WorkingMemoryTier instance
        episodic_memory:
            EpisodicMemory for storing summaries
        max_context_tokens:
            Maximum context budget (for utilization calc)
        """
        self.provider = provider
        self.working_tier = working_tier
        self.episodic_memory = episodic_memory
        self.max_context_tokens = max_context_tokens

    async def should_compact(
        self,
        turn_count: int,
        current_token_count: int,
        session_end: bool = False,
    ) -> bool:
        """Determine if compaction should be triggered.

        Triggers when:
        - Working memory turns > 30
        - Context utilization > 75%
        - Session end (always compact)

        Parameters
        ----------
        turn_count:
            Number of turns in working memory
        current_token_count:
            Current tokens used
        session_end:
            Whether this is a session-end compaction

        Returns
        -------
        bool:
            True if compaction should be performed
        """
        if session_end:
            return turn_count > self.KEEP_LAST_N_TURNS

        if turn_count > self.MIN_TURNS_FOR_COMPACTION:
            logger.debug(
                f"Compaction triggered: {turn_count} turns > {self.MIN_TURNS_FOR_COMPACTION}"
            )
            return True

        utilization = current_token_count / self.max_context_tokens
        if utilization > self.CONTEXT_UTILIZATION_THRESHOLD:
            logger.debug(
                f"Compaction triggered: {utilization:.1%} utilization > {self.CONTEXT_UTILIZATION_THRESHOLD:.0%}"
            )
            return True

        return False

    async def compact(
        self,
        session_id: str,
        turns_to_summarize: list[Any] | None = None,
    ) -> CompactionStats | None:
        """Perform context compaction.

        Takes oldest N turns, summarizes them, replaces with summary.

        Parameters
        ----------
        session_id:
            Session ID for tracking
        turns_to_summarize:
            Specific turns to compact. If None, uses working memory.

        Returns
        -------
        CompactionStats | None:
            Compaction statistics if successful, None if failed
        """
        if turns_to_summarize is None:
            # Get turns from working memory
            all_turns = self.working_tier.get_turns()
            if len(all_turns) <= self.KEEP_LAST_N_TURNS:
                logger.debug("Not enough turns for compaction")
                return None

            turns_to_summarize = all_turns[: len(all_turns) - self.KEEP_LAST_N_TURNS]

        if not turns_to_summarize:
            return None

        # Calculate original metrics
        original_turn_count = len(turns_to_summarize)
        original_token_count = self._estimate_tokens(turns_to_summarize)

        logger.debug(f"Compacting {original_turn_count} turns ({original_token_count} tokens)")

        # Generate summary
        summary = await self._generate_summary(turns_to_summarize)
        if not summary:
            logger.warning("Failed to generate summary")
            return None

        # Validate summary quality
        if not self._validate_summary(summary, original_token_count):
            logger.warning("Summary failed quality check")
            return None

        # Store summary in episodic memory. EpisodicMemory.record_turn()'s
        # schema has no metadata column (unlike the legacy EpisodicMemoryTier),
        # so the compaction tag/counts live only in the working-memory turn
        # _replace_turns_with_summary() adds below, not here.
        summary_token_count = self._estimate_tokens_string(summary)
        try:
            await self.episodic_memory.record_turn(
                session_id=session_id,
                role="system",
                content=summary,
                tokens=summary_token_count,
            )
        except Exception as e:
            logger.warning(f"Failed to store compaction summary: {e}")
            return None

        # Replace old turns in working memory with summary
        self._replace_turns_with_summary(turns_to_summarize, summary)

        compression_ratio = (
            original_token_count / summary_token_count if summary_token_count > 0 else 0
        )

        stats = CompactionStats(
            original_turn_count=original_turn_count,
            original_token_count=original_token_count,
            summary_token_count=summary_token_count,
            compression_ratio=compression_ratio,
            turns_compacted=original_turn_count,
            turns_kept=len(self.working_tier.get_turns()),
            timestamp=time.time(),
        )

        logger.info(
            f"Compaction complete: {original_turn_count} turns → {summary_token_count} tokens "
            f"({compression_ratio:.1f}x compression)"
        )

        return stats

    async def _generate_summary(self, turns: list[Any]) -> str | None:
        """Generate a summary of conversation turns.

        Parameters
        ----------
        turns:
            List of MemoryTurn objects to summarize

        Returns
        -------
        str | None:
            Summary text or None if generation failed
        """
        from velune.core.types.inference import InferenceRequest

        # Format turns for the model
        conversation_text = self._format_turns(turns)

        # Create summarization request
        prompt = f"""You are compacting a conversation about software development.

Extract and preserve the following:
- Specific files mentioned
- Bugs found or issues identified
- Decisions made
- Code changes implemented
- Unresolved questions or open items
- Key insights or learning outcomes

Format as a structured bullet list. Omit pleasantries and filler.
Do NOT include implementation details that are now in the codebase (focus on decisions).

Conversation to summarize:
{conversation_text}

Provide a compact summary:"""

        try:
            request = InferenceRequest(
                model_id=self.provider.default_model
                if hasattr(self.provider, "default_model")
                else "gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1000,
            )

            response = await self.provider.infer(request)
            return response.content if response else None

        except Exception as e:
            logger.error(f"Failed to generate summary: {e}")
            return None

    def _validate_summary(self, summary: str, original_token_count: int) -> bool:
        """Validate summary quality.

        Checks:
        - Is at least 100 characters (not empty/degenerate)
        - Does not contain model refusals
        - Is at most 30% of original token count

        Parameters
        ----------
        summary:
            Generated summary text
        original_token_count:
            Token count of original turns

        Returns
        -------
        bool:
            True if summary passes all quality checks
        """
        # Check minimum length
        if len(summary) < self.MIN_SUMMARY_LENGTH:
            logger.warning(f"Summary too short: {len(summary)} < {self.MIN_SUMMARY_LENGTH}")
            return False

        # Check for model refusals
        refusal_indicators = [
            "i cannot",
            "i don't have access",
            "i'm unable to",
            "i don't have permission",
            "unable to access",
        ]
        summary_lower = summary.lower()
        for indicator in refusal_indicators:
            if indicator in summary_lower:
                logger.warning(f"Summary contains refusal indicator: {indicator}")
                return False

        # Check compression ratio
        summary_token_count = self._estimate_tokens_string(summary)
        max_allowed = original_token_count * self.MAX_SUMMARY_SIZE_RATIO

        if summary_token_count > max_allowed:
            logger.warning(
                f"Summary too large: {summary_token_count} > {max_allowed:.0f} "
                f"({summary_token_count / original_token_count:.1%} of original)"
            )
            return False

        return True

    def _format_turns(self, turns: list[Any]) -> str:
        """Format turns into readable conversation text.

        Parameters
        ----------
        turns:
            List of MemoryTurn objects

        Returns
        -------
        str:
            Formatted conversation text
        """
        lines = []
        for turn in turns:
            role = turn.role.capitalize()
            content = turn.content
            # Truncate very long turns
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"{role}: {content}")

        return "\n".join(lines)

    def _replace_turns_with_summary(self, turns: list[Any], summary: str) -> None:
        """Replace old turns in working memory with summary.

        Parameters
        ----------
        turns:
            List of turns to remove
        summary:
            Summary text to add
        """
        # Get indices of turns to remove
        all_turns = self.working_tier._turns
        indices_to_remove = []

        for turn in turns:
            for i, t in enumerate(all_turns):
                if t == turn:
                    indices_to_remove.append(i)
                    break

        # Remove in reverse order to maintain indices
        for i in sorted(indices_to_remove, reverse=True):
            all_turns.pop(i)

        # Add summary as system message
        self.working_tier.add_turn(
            role="system",
            content=summary,
            metadata={"type": "compaction_summary"},
        )

        logger.debug(f"Replaced {len(turns)} turns with summary in working memory")

    def _estimate_tokens(self, turns: list[Any]) -> int:
        """Estimate token count for a list of turns.

        Uses rough approximation: ~1 token per 4 characters.

        Parameters
        ----------
        turns:
            List of MemoryTurn objects

        Returns
        -------
        int:
            Estimated token count
        """
        total_chars = sum(len(turn.content) for turn in turns)
        return max(1, total_chars // 4)

    def _estimate_tokens_string(self, text: str) -> int:
        """Estimate token count for a string.

        Uses rough approximation: ~1 token per 4 characters.

        Parameters
        ----------
        text:
            Text to estimate tokens for

        Returns
        -------
        int:
            Estimated token count
        """
        return max(1, len(text) // 4)


