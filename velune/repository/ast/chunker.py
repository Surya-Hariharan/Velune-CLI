"""AST-aware semantic chunking."""

from pathlib import Path
from typing import list
from velune.core.types import ContextChunk, ContextPriority
import time


class ASTAwareChunker:
    """Chunks code based on AST structure."""

    def __init__(self):
        pass

    def chunk_file(self, file_path: Path, code: str) -> list[ContextChunk]:
        """Chunk a file into semantic units."""
        chunks = []
        
        # Chunk by function/class boundaries
        lines = code.split("\n")
        current_chunk = []
        chunk_start = 0
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # Start of function/class
            if stripped.startswith("def ") or stripped.startswith("class "):
                if current_chunk:
                    # Save previous chunk
                    chunk_text = "\n".join(current_chunk)
                    chunks.append(self._create_chunk(chunk_text, file_path, chunk_start))
                    current_chunk = []
                    chunk_start = i
            
            current_chunk.append(line)
        
        # Save final chunk
        if current_chunk:
            chunk_text = "\n".join(current_chunk)
            chunks.append(self._create_chunk(chunk_text, file_path, chunk_start))
        
        return chunks

    def _create_chunk(
        self,
        content: str,
        file_path: Path,
        line_start: int,
    ) -> ContextChunk:
        """Create a context chunk."""
        return ContextChunk(
            content=content,
            source=str(file_path),
            priority=ContextPriority.MEDIUM,
            tokens=len(content) // 4,
            relevance_score=0.5,
            timestamp=time.time(),
            metadata={"line_start": line_start},
        )
