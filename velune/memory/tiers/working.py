"""Working Memory Tier (Tier 1).

Fast, in-process storage for the active session, conversation turns,
and transient execution logs.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("velune.memory.tiers.working")


class MemoryTurn(BaseModel):
    """A single turn in the working memory."""
    role: str
    content: str
    timestamp: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkingMemoryTier:
    """Tier 1: Fast, in-memory transient store for the active session."""

    def __init__(self) -> None:
        self._turns: list[MemoryTurn] = []
        self._state: dict[str, Any] = {}
        self._execution_logs: list[dict[str, Any]] = []

    def add_turn(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Add a conversation turn to working memory."""
        turn = MemoryTurn(role=role, content=content, metadata=metadata or {})
        self._turns.append(turn)
        logger.debug("Added turn to working memory: %s", role)

    def get_turns(self) -> list[MemoryTurn]:
        """Get all turns in chronological order."""
        return list(self._turns)

    def get_recent_turns(self, limit: int = 10) -> list[MemoryTurn]:
        """Get the N most recent conversation turns."""
        return self._turns[-limit:]

    def update_state(self, key: str, value: Any) -> None:
        """Update transient state variables."""
        self._state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        """Retrieve a transient state variable."""
        return self._state.get(key, default)

    def log_execution_step(self, step_name: str, payload: dict[str, Any]) -> None:
        """Record a transient execution step log."""
        self._execution_logs.append({
            "step": step_name,
            "payload": payload,
            "timestamp": time.time(),
        })

    def get_execution_logs(self) -> list[dict[str, Any]]:
        """Get all transient execution logs."""
        return list(self._execution_logs)

    def clear(self) -> None:
        """Clear all active working memory structures."""
        self._turns.clear()
        self._state.clear()
        self._execution_logs.clear()
        logger.info("Cleared working memory tier.")
