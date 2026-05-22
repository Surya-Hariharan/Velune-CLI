"""Checkpoint persistence for resumable orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from velune.orchestration.schemas import OrchestrationState


@dataclass(slots=True)
class CheckpointRecord:
    """Snapshot record for a graph transition boundary."""

    checkpoint_id: str
    run_id: str
    node_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    state: OrchestrationState | None = None


class InMemoryCheckpointStore:
    """Stores orchestration checkpoints for resume and diagnostics."""

    def __init__(self) -> None:
        self._records: dict[str, list[CheckpointRecord]] = {}

    def save(self, run_id: str, node_name: str, state: OrchestrationState) -> str:
        sequence = len(self._records.get(run_id, [])) + 1
        checkpoint_id = f"{run_id}:cp:{sequence:04d}"
        record = CheckpointRecord(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            node_name=node_name,
            state=deepcopy(state),
        )
        self._records.setdefault(run_id, []).append(record)
        return checkpoint_id

    def latest(self, run_id: str) -> OrchestrationState | None:
        records = self._records.get(run_id, [])
        if not records:
            return None
        return deepcopy(records[-1].state)

    def list_ids(self, run_id: str) -> list[str]:
        return [record.checkpoint_id for record in self._records.get(run_id, [])]

    def load(self, run_id: str, checkpoint_id: str) -> OrchestrationState | None:
        for record in self._records.get(run_id, []):
            if record.checkpoint_id == checkpoint_id:
                return deepcopy(record.state)
        return None
