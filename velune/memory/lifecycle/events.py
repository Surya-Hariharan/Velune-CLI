"""Memory-triggered event emissions."""

from typing import Any, Callable
from velune.core.types import MemoryRecord


class MemoryEventEmitter:
    """Emits events based on memory operations."""

    def __init__(self):
        self._listeners: dict[str, list[Callable]] = {}

    def on_memory_added(self, callback: Callable[[MemoryRecord], None]) -> None:
        """Register a callback for memory addition events."""
        if "memory_added" not in self._listeners:
            self._listeners["memory_added"] = []
        self._listeners["memory_added"].append(callback)

    def on_memory_accessed(self, callback: Callable[[MemoryRecord], None]) -> None:
        """Register a callback for memory access events."""
        if "memory_accessed" not in self._listeners:
            self._listeners["memory_accessed"] = []
        self._listeners["memory_accessed"].append(callback)

    def on_memory_consolidated(self, callback: Callable[[dict[str, int]], None]) -> None:
        """Register a callback for memory consolidation events."""
        if "memory_consolidated" not in self._listeners:
            self._listeners["memory_consolidated"] = []
        self._listeners["memory_consolidated"].append(callback)

    def emit_memory_added(self, record: MemoryRecord) -> None:
        """Emit a memory added event."""
        for callback in self._listeners.get("memory_added", []):
            callback(record)

    def emit_memory_accessed(self, record: MemoryRecord) -> None:
        """Emit a memory accessed event."""
        for callback in self._listeners.get("memory_accessed", []):
            callback(record)

    def emit_memory_consolidated(self, stats: dict[str, int]) -> None:
        """Emit a memory consolidated event."""
        for callback in self._listeners.get("memory_consolidated", []):
            callback(stats)
