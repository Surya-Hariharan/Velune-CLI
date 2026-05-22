"""Memory-related errors."""


class MemoryError(Exception):
    """Base exception for memory errors."""
    pass


class MemoryStoreError(MemoryError):
    """Raised when memory store operation fails."""
    pass


class MemoryRetrievalError(MemoryError):
    """Raised when memory retrieval fails."""
    pass


class MemoryConsolidationError(MemoryError):
    """Raised when memory consolidation fails."""
    pass
