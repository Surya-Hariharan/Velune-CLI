"""Reconstructed context validation."""

from typing import list
from velune.core.types import ContextChunk, ContextWindow


class ContextValidator:
    """Validates reconstructed context."""

    def validate(self, chunks: list[ContextChunk]) -> dict[str, Any]:
        """Validate a list of context chunks."""
        issues = []
        
        # Check for empty chunks
        empty_chunks = [c for c in chunks if not c.content.strip()]
        if empty_chunks:
            issues.append(f"Found {len(empty_chunks)} empty chunks")
        
        # Check for duplicate content
        content_hashes = {}
        duplicates = []
        for chunk in chunks:
            content_hash = hash(chunk.content)
            if content_hash in content_hashes:
                duplicates.append(chunk.source)
            content_hashes[content_hash] = chunk.source
        
        if duplicates:
            issues.append(f"Found {len(duplicates)} duplicate chunks")
        
        # Check for very long chunks
        long_chunks = [c for c in chunks if c.tokens > 5000]
        if long_chunks:
            issues.append(f"Found {len(long_chunks)} very long chunks (>5000 tokens)")
        
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "chunk_count": len(chunks),
            "total_tokens": sum(c.tokens for c in chunks),
        }

    def validate_window(self, window: ContextWindow) -> dict[str, Any]:
        """Validate a context window."""
        issues = []
        
        # Check utilization
        if window.utilization > 1.0:
            issues.append(f"Context window exceeds max tokens: {window.total_tokens} > {window.max_tokens}")
        
        # Check compression ratio
        if window.compression_ratio < 0.5:
            issues.append(f"High compression ratio: {window.compression_ratio:.2f}")
        
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "utilization": window.utilization,
            "compression_ratio": window.compression_ratio,
        }
