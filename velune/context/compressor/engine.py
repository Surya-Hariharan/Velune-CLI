"""Context compression pipeline."""

from typing import list
from velune.core.types import ContextChunk
from velune.context.compressor.summarizer import ChunkSummarizer
from velune.context.compressor.selector import ChunkSelector


class CompressionEngine:
    """Engine for compressing context chunks."""

    def __init__(self):
        self.summarizer = ChunkSummarizer()
        self.selector = ChunkSelector()

    def compress(
        self,
        chunks: list[ContextChunk],
        target_tokens: int,
    ) -> list[ContextChunk]:
        """Compress chunks to fit target token budget."""
        current_tokens = sum(chunk.tokens for chunk in chunks)
        
        if current_tokens <= target_tokens:
            return chunks
        
        # Select chunks to keep
        selected = self.selector.select(chunks, target_tokens)
        
        # Summarize if needed
        compressed = []
        for chunk in selected:
            if chunk.tokens > 1000:  # Summarize large chunks
                summarized = self.summarizer.summarize(chunk)
                compressed.append(summarized)
            else:
                compressed.append(chunk)
        
        return compressed
