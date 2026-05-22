"""Importance-based chunk selection."""

from typing import list
from velune.core.types import ContextChunk


class ChunkSelector:
    """Selects chunks based on importance."""

    def select(
        self,
        chunks: list[ContextChunk],
        target_tokens: int,
    ) -> list[ContextChunk]:
        """Select chunks to fit within target tokens."""
        # Sort by priority and relevance
        sorted_chunks = sorted(
            chunks,
            key=lambda c: (c.priority.value, c.relevance_score),
            reverse=True,
        )
        
        selected = []
        total_tokens = 0
        
        for chunk in sorted_chunks:
            if total_tokens + chunk.tokens <= target_tokens:
                selected.append(chunk)
                total_tokens += chunk.tokens
            elif total_tokens < target_tokens:
                # Partial chunk
                remaining = target_tokens - total_tokens
                ratio = remaining / chunk.tokens
                partial_chunk = ContextChunk(
                    content=chunk.content[:int(len(chunk.content) * ratio)],
                    source=chunk.source,
                    priority=chunk.priority,
                    tokens=remaining,
                    relevance_score=chunk.relevance_score,
                    timestamp=chunk.timestamp,
                    metadata={"partial": True, **chunk.metadata},
                )
                selected.append(partial_chunk)
                total_tokens += remaining
            else:
                break
        
        return selected
