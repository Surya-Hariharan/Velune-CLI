"""Context assembly from ranked chunks."""

from typing import Dict
from velune.core.types import ContextWindow, ContextChunk


class ContextAssembler:
    """Assembles context window from ranked chunks."""

    def assemble(
        self,
        chunks: list[ContextChunk],
        budget: Dict[str, int],
        max_tokens: int,
    ) -> ContextWindow:
        """Assemble context window from chunks respecting budget."""
        # Sort chunks by priority and relevance
        sorted_chunks = self._sort_chunks(chunks)
        
        # Select chunks within budget
        selected_chunks = self._select_chunks(sorted_chunks, budget)
        
        # Calculate total tokens
        total_tokens = sum(chunk.tokens for chunk in selected_chunks)
        
        # Calculate compression ratio if needed
        compression_ratio = 1.0
        if total_tokens > max_tokens:
            compression_ratio = max_tokens / total_tokens
            selected_chunks = self._compress_chunks(selected_chunks, max_tokens)
            total_tokens = sum(chunk.tokens for chunk in selected_chunks)
        
        return ContextWindow(
            chunks=selected_chunks,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            compression_ratio=compression_ratio,
        )

    def _sort_chunks(self, chunks: list[ContextChunk]) -> list[ContextChunk]:
        """Sort chunks by priority and relevance."""
        priority_order = {
            "critical": 0,
            "high": 1,
            "medium": 2,
            "low": 3,
        }
        
        return sorted(
            chunks,
            key=lambda c: (
                priority_order.get(c.priority.value, 99),
                -c.relevance_score,
            ),
        )

    def _select_chunks(
        self,
        chunks: list[ContextChunk],
        budget: Dict[str, int],
    ) -> list[ContextChunk]:
        """Select chunks within budget per priority tier."""
        selected = []
        tier_usage = {tier: 0 for tier in budget}
        
        for chunk in chunks:
            tier = chunk.priority.value
            if tier in budget:
                if tier_usage[tier] + chunk.tokens <= budget[tier]:
                    selected.append(chunk)
                    tier_usage[tier] += chunk.tokens
        
        return selected

    def _compress_chunks(
        self,
        chunks: list[ContextChunk],
        max_tokens: int,
    ) -> list[ContextChunk]:
        """Compress chunks to fit within max tokens."""
        compressed = []
        total = 0
        
        for chunk in chunks:
            if total + chunk.tokens <= max_tokens:
                compressed.append(chunk)
                total += chunk.tokens
            else:
                remaining = max_tokens - total
                if remaining > 0:
                    # Truncate chunk
                    ratio = remaining / chunk.tokens
                    compressed_chunk = ContextChunk(
                        content=chunk.content[:int(len(chunk.content) * ratio)],
                        source=chunk.source,
                        priority=chunk.priority,
                        tokens=remaining,
                        relevance_score=chunk.relevance_score,
                        timestamp=chunk.timestamp,
                        metadata=chunk.metadata,
                    )
                    compressed.append(compressed_chunk)
                break
        
        return compressed
