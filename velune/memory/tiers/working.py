"""Working Memory Tier (Tier 1).

Fast, in-process storage for the active session, conversation turns,
and transient execution logs.

Phase 1 repairs:
  * Session isolation: each ``WorkingMemoryTier`` instance is bound to an
    explicit ``session_id``.  Only turns belonging to that session are
    returned by ``get_turns()`` and ``get_recent_turns()``.
  * TTL eviction: turns older than ``ttl_seconds`` are treated as expired
    and removed by ``evict_expired()``.  The lifecycle coordinator can call
    this on shutdown or before each flush to episodic SQLite.
  * ``is_expired()`` returns True if *all* turns in the session have aged
    past the TTL, allowing the lifecycle to reclaim dead sessions.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("velune.memory.tiers.working")

# Default TTL: 2 hours.  Callers may pass a tighter or looser value.
_DEFAULT_TTL_SECONDS: float = 7200.0


class MemoryTurn(BaseModel):
    """A single turn in the working memory."""

    role: str
    content: str
    timestamp: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str = Field(default="default")


class WorkingMemoryTier:
    """Tier 1: Fast, in-memory transient store for the active session.

    Parameters
    ----------
    session_id:
        Logical identifier for this session.  Used to namespace turns so
        that multiple ``WorkingMemoryTier`` instances within the same
        process never accidentally share data.
    ttl_seconds:
        How long (in wall-clock seconds) a turn is considered live.
        Turns older than this are removed by :meth:`evict_expired`.
        Defaults to 2 hours.
    """

    def __init__(
        self,
        session_id: str = "default",
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._session_id = session_id
        self._ttl_seconds = ttl_seconds
        self._turns: list[MemoryTurn] = []
        self._state: dict[str, Any] = {}
        self._execution_logs: list[dict[str, Any]] = []
        self._created_at: float = time.time()

    # ------------------------------------------------------------------
    # Session metadata
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """The session this tier is bound to."""
        return self._session_id

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------

    def add_turn(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Add a conversation turn to working memory."""
        turn = MemoryTurn(
            role=role,
            content=content,
            metadata=metadata or {},
            session_id=self._session_id,
        )
        self._turns.append(turn)
        logger.debug("Added turn to working memory [session=%s role=%s]", self._session_id, role)

    def get_turns(self) -> list[MemoryTurn]:
        """Get all turns for this session in chronological order."""
        return [t for t in self._turns if t.session_id == self._session_id]

    def get_recent_turns(self, limit: int = 10) -> list[MemoryTurn]:
        """Get the N most recent conversation turns for this session."""
        session_turns = self.get_turns()
        return session_turns[-limit:]

    # ------------------------------------------------------------------
    # TTL eviction
    # ------------------------------------------------------------------

    def evict_expired(self) -> int:
        """Remove all turns that have exceeded the session TTL.

        Returns the number of turns evicted.  Safe to call at any time;
        active turns are never removed.
        """
        cutoff = time.time() - self._ttl_seconds
        before = len(self._turns)
        self._turns = [t for t in self._turns if t.timestamp >= cutoff]
        evicted = before - len(self._turns)
        if evicted:
            logger.debug(
                "Evicted %d expired turns from working memory [session=%s ttl=%.0fs]",
                evicted,
                self._session_id,
                self._ttl_seconds,
            )
        return evicted

    def is_expired(self) -> bool:
        """Return True if this session has no live turns (all aged past TTL).

        A freshly created session with zero turns is NOT considered expired —
        the caller must check :meth:`get_turns` to distinguish empty-new from
        empty-evicted.
        """
        if not self._turns:
            # No turns yet; treat as live (session may still be initialising)
            return False
        cutoff = time.time() - self._ttl_seconds
        return all(t.timestamp < cutoff for t in self._turns)

    # ------------------------------------------------------------------
    # State / execution log helpers (unchanged semantics)
    # ------------------------------------------------------------------

    def update_state(self, key: str, value: Any) -> None:
        """Update transient state variables."""
        self._state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        """Retrieve a transient state variable."""
        return self._state.get(key, default)

    def log_execution_step(self, step_name: str, payload: dict[str, Any]) -> None:
        """Record a transient execution step log."""
        self._execution_logs.append(
            {
                "step": step_name,
                "payload": payload,
                "timestamp": time.time(),
            }
        )

    def get_execution_logs(self) -> list[dict[str, Any]]:
        """Get all transient execution logs."""
        return list(self._execution_logs)

    def clear(self) -> None:
        """Clear all active working memory structures."""
        self._turns.clear()
        self._state.clear()
        self._execution_logs.clear()
        logger.info("Cleared working memory tier [session=%s].", self._session_id)
