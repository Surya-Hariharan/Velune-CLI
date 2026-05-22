"""Memory lifecycle policy and decay service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional


@dataclass(slots=True)
class MemoryArtifact:
    """Tracked memory artifact."""

    id: str
    memory_type: str
    content: str
    importance: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryLifecycleService:
    """Controls retention, decay, and promotion across memory tiers."""

    def __init__(self, decay_half_life_days: int = 14) -> None:
        self.decay_half_life_days = decay_half_life_days
        self._artifacts: dict[str, MemoryArtifact] = {}

    def ingest(self, artifact: MemoryArtifact) -> None:
        self._artifacts[artifact.id] = artifact

    def promote(self, artifact_id: str, importance_delta: float = 0.1) -> Optional[MemoryArtifact]:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return None
        artifact.importance = min(1.0, artifact.importance + importance_delta)
        artifact.last_accessed_at = datetime.now(tz=UTC)
        return artifact

    def decay(self) -> None:
        now = datetime.now(tz=UTC)
        for artifact in self._artifacts.values():
            age_days = max((now - artifact.last_accessed_at).total_seconds() / 86_400, 0)
            decay = 0.5 ** (age_days / max(self.decay_half_life_days, 1))
            artifact.importance = max(0.0, artifact.importance * decay)

    def prune(self, threshold: float = 0.05) -> list[str]:
        removed: list[str] = []
        for artifact_id, artifact in list(self._artifacts.items()):
            if artifact.importance < threshold:
                removed.append(artifact_id)
                del self._artifacts[artifact_id]
        return removed

    def list(self, memory_type: Optional[str] = None) -> list[MemoryArtifact]:
        if memory_type is None:
            return list(self._artifacts.values())
        return [artifact for artifact in self._artifacts.values() if artifact.memory_type == memory_type]

    def summary(self) -> dict[str, Any]:
        return {
            "total": len(self._artifacts),
            "memory_types": sorted({artifact.memory_type for artifact in self._artifacts.values()}),
        }