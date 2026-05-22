"""Cognitive model updater."""

from pathlib import Path
from velune.workspace.cognition.model import LiveCognitionModel
from velune.repository.schemas import RepositorySnapshot


class CognitionModelUpdater:
    """Updates the cognitive model based on repository state."""

    def __init__(self, cognition_model: LiveCognitionModel):
        self.cognition_model = cognition_model

    def update_from_repository(self, repo_model: RepositorySnapshot) -> None:
        """Update cognition model from repository snapshot."""
        file_count = len(repo_model.files)
        symbol_count = len(repo_model.symbols)
        self.cognition_model.update_index_stats(
            file_count=file_count,
            symbol_count=symbol_count,
        )

    def update_health(self, repo_model: RepositorySnapshot) -> None:
        """Update health score."""
        # Calculate overall score based on the violations reported in summary
        violations = repo_model.summary.get("architecture", {}).get("violations_count", 0)
        score = max(0.0, 100.0 - (violations * 5.0))
        self.cognition_model.update_health_score(score)

