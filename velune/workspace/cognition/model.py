"""Live cognitive workspace model."""

from datetime import datetime
from pathlib import Path

from velune.core.types import CognitionModel, WorkspaceState


class LiveCognitionModel:
    """Live cognitive model of the workspace."""

    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path
        self.state = WorkspaceState.IDLE
        self.current_task_id: str | None = None
        self.file_count = 0
        self.symbol_count = 0
        self.last_indexed: datetime | None = None
        self.health_score = 0.0
        self._metadata: dict[str, any] = {}

    def update_state(self, new_state: WorkspaceState) -> None:
        """Update workspace state."""
        self.state = new_state

    def set_task(self, task_id: str) -> None:
        """Set current task."""
        self.current_task_id = task_id

    def clear_task(self) -> None:
        """Clear current task."""
        self.current_task_id = None

    def update_index_stats(self, file_count: int, symbol_count: int) -> None:
        """Update indexing statistics."""
        self.file_count = file_count
        self.symbol_count = symbol_count
        self.last_indexed = datetime.now()

    def update_health_score(self, score: float) -> None:
        """Update health score."""
        self.health_score = score

    def set_metadata(self, key: str, value: any) -> None:
        """Set metadata."""
        self._metadata[key] = value

    def get_metadata(self, key: str) -> any | None:
        """Get metadata."""
        return self._metadata.get(key)

    def to_model(self) -> CognitionModel:
        """Convert to CognitionModel."""
        return CognitionModel(
            workspace_path=str(self.workspace_path),
            state=self.state,
            current_task_id=self.current_task_id,
            file_count=self.file_count,
            symbol_count=self.symbol_count,
            last_indexed=self.last_indexed,
            health_score=self.health_score,
            metadata=self._metadata.copy(),
        )
