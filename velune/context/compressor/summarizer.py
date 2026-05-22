"""LLM-based chunk summarization."""

from velune.core.types import ContextChunk


class ChunkSummarizer:
    """Summarizes context chunks."""

    def __init__(self):
        # In production, this would use an LLM
        pass

    def summarize(self, chunk: ContextChunk) -> ContextChunk:
        """Summarize a context chunk."""
        # Simple summarization: take first and last sentences
        sentences = chunk.content.split(". ")
        
        if len(sentences) <= 2:
            return chunk
        
        summarized_content = sentences[0] + ". " + sentences[-1] + "."
        
        # Estimate new token count
        new_tokens = len(summarized_content) // 4
        
        return ContextChunk(
            content=summarized_content,
            source=chunk.source,
            priority=chunk.priority,
            tokens=new_tokens,
            relevance_score=chunk.relevance_score * 0.9,  # Slightly lower relevance
            timestamp=chunk.timestamp,
            metadata={"summarized": True, **chunk.metadata},
        )
