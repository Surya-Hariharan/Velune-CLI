"""Cognitive model query interface."""

from typing import Optional, list
from velune.workspace.cognition.model import LiveCognitionModel


class CognitionModelQueries:
    """Query interface for the cognitive model."""

    def __init__(self, model: LiveCognitionModel):
        self.model = model

    def is_idle(self) -> bool:
        """Check if workspace is idle."""
        return self.model.state.value == "idle"

    def is_busy(self) -> bool:
        """Check if workspace is busy with a task."""
        return self.model.state.value in ["task_active", "debugging", "reviewing", "indexing"]

    def has_active_task(self) -> bool:
        """Check if there's an active task."""
        return self.model.current_task_id is not None

    def get_task_id(self) -> Optional[str]:
        """Get current task ID."""
        return self.model.current_task_id

    def get_file_count(self) -> int:
        """Get indexed file count."""
        return self.model.file_count

    def get_symbol_count(self) -> int:
        """Get indexed symbol count."""
        return self.model.symbol_count

    def get_health_score(self) -> float:
        """Get health score."""
        return self.model.health_score

    def is_healthy(self) -> bool:
        """Check if workspace is healthy."""
        return self.model.health_score >= 0.7
