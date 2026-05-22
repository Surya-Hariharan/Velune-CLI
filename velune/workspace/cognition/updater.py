"""Cognitive model updater."""

from pathlib import Path
from velune.workspace.cognition.model import LiveCognitionModel
from velune.repository.cognition.model import RepositoryCognitiveModel
from velune.repository.cognition.health import RepositoryHealthAnalyzer


class CognitionModelUpdater:
    """Updates the cognitive model based on repository state."""

    def __init__(self, cognition_model: LiveCognitionModel):
        self.cognition_model = cognition_model

    def update_from_repository(self, repo_model: RepositoryCognitiveModel) -> None:
        """Update cognition model from repository model."""
        stats = repo_model.get_statistics()
        self.cognition_model.update_index_stats(
            file_count=stats["file_count"],
            symbol_count=stats["symbol_count"],
        )

    def update_health(self, repo_model: RepositoryCognitiveModel) -> None:
        """Update health score."""
        analyzer = RepositoryHealthAnalyzer(repo_model)
        health = analyzer.analyze()
        self.cognition_model.update_health_score(health["overall_score"])
