"""Typed cognitive event dataclasses for every Velune cognition lifecycle stage.

Every significant action in the cognition system emits a typed event.
Events are immutable, carry full correlation metadata, and flow through
the ``CognitionEventBus`` for routing, replay, and audit.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Base event
# ─────────────────────────────────────────────────────────────────────────────

def _new_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class CognitionEvent:
    """Base class for all Velune cognitive events.

    Attributes:
        event_id: Globally unique event identifier.
        topic: Dot-separated topic path (e.g. ``velune.council.started``).
        session_id: Logical session this event belongs to.
        trace_id: Cross-component trace correlation id.
        timestamp_ns: Wall-clock nanoseconds since Unix epoch.
        payload: Arbitrary serialisable metadata.
    """

    topic: str
    session_id: str
    event_id: str = field(default_factory=_new_id)
    trace_id: str = field(default_factory=_new_id)
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-compatible dictionary."""
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "timestamp_ns": self.timestamp_ns,
            "payload": self.payload,
            "event_class": type(self).__name__,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Execution events  (velune.execution.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ExecutionStarted(CognitionEvent):
    """Fired when a task execution pass begins."""
    topic: str = field(default="velune.execution.started", init=False)


@dataclass(frozen=True, slots=True)
class ExecutionCompleted(CognitionEvent):
    """Fired when a task execution pass completes successfully."""
    topic: str = field(default="velune.execution.completed", init=False)


@dataclass(frozen=True, slots=True)
class ExecutionFailed(CognitionEvent):
    """Fired when a task execution pass fails or is rolled back."""
    topic: str = field(default="velune.execution.failed", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Council / deliberation events  (velune.cognition.council.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CouncilStarted(CognitionEvent):
    """Fired when the multi-agent Reasoning Council begins deliberation."""
    topic: str = field(default="velune.cognition.council.started", init=False)


@dataclass(frozen=True, slots=True)
class CouncilEnded(CognitionEvent):
    """Fired when the council has produced a final arbitration result."""
    topic: str = field(default="velune.cognition.council.ended", init=False)


@dataclass(frozen=True, slots=True)
class CouncilDebateRound(CognitionEvent):
    """Fired at the start of each contradiction-driven debate turn."""
    topic: str = field(default="velune.cognition.council.debate_round", init=False)


@dataclass(frozen=True, slots=True)
class CouncilFastPath(CognitionEvent):
    """Fired when the simple task fast-path is taken instead of full council."""
    topic: str = field(default="velune.cognition.council.fast_path", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Memory events  (velune.cognition.memory.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MemoryUpdated(CognitionEvent):
    """Fired when the lineage DB write queue commits a batch."""
    topic: str = field(default="velune.cognition.memory.updated", init=False)


@dataclass(frozen=True, slots=True)
class MemoryQueried(CognitionEvent):
    """Fired when a continuity/lineage query is issued."""
    topic: str = field(default="velune.cognition.memory.queried", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Personality events  (velune.cognition.personality.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PersonalityRefreshed(CognitionEvent):
    """Fired when a style profile is re-scanned from AST."""
    topic: str = field(default="velune.cognition.personality.refreshed", init=False)


@dataclass(frozen=True, slots=True)
class PersonalityCacheHit(CognitionEvent):
    """Fired when a cached style profile is used without re-scanning."""
    topic: str = field(default="velune.cognition.personality.cache_hit", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Architecture events  (velune.architecture.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ArchitectureDebtRaised(CognitionEvent):
    """Fired when a new debt item is added to the Cognitive Debt Ledger."""
    topic: str = field(default="velune.architecture.debt.raised", init=False)


@dataclass(frozen=True, slots=True)
class ArchitectureDriftAlarm(CognitionEvent):
    """Fired when the Architecture Drift Alarm (ADA) detects a layering violation."""
    topic: str = field(default="velune.architecture.alarm.triggered", init=False)


@dataclass(frozen=True, slots=True)
class ArchitectureAuditCompleted(CognitionEvent):
    """Fired when the ArchitectureCognitionAgent finishes an audit pass."""
    topic: str = field(default="velune.architecture.audit.completed", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Repository events  (velune.repository.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RepositoryChanged(CognitionEvent):
    """Fired when a filesystem change is detected in the workspace."""
    topic: str = field(default="velune.repository.changed", init=False)


@dataclass(frozen=True, slots=True)
class RepositoryIndexed(CognitionEvent):
    """Fired when the repository AST index scan completes."""
    topic: str = field(default="velune.repository.indexed", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Trade-off Evaluation events  (velune.tem.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TemEvaluated(CognitionEvent):
    """Fired when the Trade-off Evaluation Matrix produces a decision."""
    topic: str = field(default="velune.tem.evaluated", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Evolution events  (velune.evolution.*)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EvolutionSnapshotTaken(CognitionEvent):
    """Fired when a monthly architecture snapshot is committed."""
    topic: str = field(default="velune.evolution.snapshot", init=False)


# ─────────────────────────────────────────────────────────────────────────────
# Topic registry (used by bus router for prefix matching)
# ─────────────────────────────────────────────────────────────────────────────

#: Complete set of all known topic strings — used for validation & `/events` slash command.
ALL_TOPICS: list[str] = [
    "velune.execution.started",
    "velune.execution.completed",
    "velune.execution.failed",
    "velune.cognition.council.started",
    "velune.cognition.council.ended",
    "velune.cognition.council.debate_round",
    "velune.cognition.council.fast_path",
    "velune.cognition.memory.updated",
    "velune.cognition.memory.queried",
    "velune.cognition.personality.refreshed",
    "velune.cognition.personality.cache_hit",
    "velune.architecture.debt.raised",
    "velune.architecture.alarm.triggered",
    "velune.architecture.audit.completed",
    "velune.repository.changed",
    "velune.repository.indexed",
    "velune.tem.evaluated",
    "velune.evolution.snapshot",
]

#: Map event class name -> class for deserialization
EVENT_CLASS_MAP: dict[str, type] = {
    "ExecutionStarted": ExecutionStarted,
    "ExecutionCompleted": ExecutionCompleted,
    "ExecutionFailed": ExecutionFailed,
    "CouncilStarted": CouncilStarted,
    "CouncilEnded": CouncilEnded,
    "CouncilDebateRound": CouncilDebateRound,
    "CouncilFastPath": CouncilFastPath,
    "MemoryUpdated": MemoryUpdated,
    "MemoryQueried": MemoryQueried,
    "PersonalityRefreshed": PersonalityRefreshed,
    "PersonalityCacheHit": PersonalityCacheHit,
    "ArchitectureDebtRaised": ArchitectureDebtRaised,
    "ArchitectureDriftAlarm": ArchitectureDriftAlarm,
    "ArchitectureAuditCompleted": ArchitectureAuditCompleted,
    "RepositoryChanged": RepositoryChanged,
    "RepositoryIndexed": RepositoryIndexed,
    "TemEvaluated": TemEvaluated,
    "EvolutionSnapshotTaken": EvolutionSnapshotTaken,
}
