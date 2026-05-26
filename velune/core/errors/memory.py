"""Memory-related errors."""


class VeluneMemoryError(Exception):
    """Base exception for memory errors."""
    pass


class VeluneMemoryStoreError(VeluneMemoryError):
    """Raised when memory store operation fails."""
    pass


class VeluneMemoryRetrievalError(VeluneMemoryError):
    """Raised when memory retrieval fails."""
    pass


class VeluneMemoryConsolidationError(VeluneMemoryError):
    """Raised when memory consolidation fails."""
    pass
