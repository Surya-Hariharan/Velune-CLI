"""Phase 2a blast radius estimator using structural importance and boundary criticality.

Combines two signals:
1. Structural importance: fan_in (how many modules depend on this)
2. Boundary criticality: whether code sits at auth/API/DB/payment boundaries

Phase 2b will add 3 more signals: git co-change history, test coverage, and architectural layers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from velune.repository.boundary_classifier import BoundaryClassifier, BoundaryType
from velune.repository.import_graph import ImportGraphBuilder, ImportMetrics

logger = logging.getLogger("velune.repository.blast_radius")


@dataclass
class BlastRadiusScore:
    """Blast radius score for a file/symbol."""

    symbol_id: str
    file_path: str
    fan_in: int
    fan_out: int
    boundary_type: BoundaryType | None
    fan_in_score: float  # 0.0-1.0: normalized fan_in
    boundary_score: float  # 0.0-1.0: boundary criticality bonus
    score: float  # 0.0-1.0: combined score (60% structural, 40% boundary)

    def __repr__(self) -> str:
        boundary_str = f" [{self.boundary_type}]" if self.boundary_type else ""
        return f"BlastRadius({self.file_path}: {self.score:.2f}{boundary_str}, fan_in={self.fan_in})"


class BlastRadiusEstimator:
    """Estimates blast radius (impact potential) of code changes.

    Uses structural metrics (fan_in/fan_out) and boundary criticality
    to determine which code locations are most important when assembling
    context for a change request.

    Scoring formula:
        score = (0.6 × fan_in_score) + (0.4 × boundary_score)
    """

    def __init__(
        self,
        import_graph: ImportGraphBuilder,
        boundary_classifier: BoundaryClassifier,
        fan_in_normalization: int = 20,
    ) -> None:
        """Initialize the estimator.

        Parameters
        ----------
        import_graph:
            The import graph builder with extracted metrics.
        boundary_classifier:
            Classifier for boundary types.
        fan_in_normalization:
            Fan-in value at which score reaches 1.0 (default: 20).
                Normalized score = min(1.0, fan_in / normalization)
        """
        self.graph = import_graph
        self.classifier = boundary_classifier
        self.fan_in_normalization = fan_in_normalization
        self._score_cache: dict[str, BlastRadiusScore] = {}

    def compute_score(self, file_path: str) -> BlastRadiusScore:
        """Compute blast radius score for a file.

        Parameters
        ----------
        file_path:
            The file path to score.

        Returns
        -------
        BlastRadiusScore:
            The computed blast radius score.
        """
        # Check cache
        if file_path in self._score_cache:
            return self._score_cache[file_path]

        # Get metrics from import graph
        metrics = self.graph.get_metrics(file_path)
        if not metrics:
            metrics = ImportMetrics(module_path=file_path)

        # Compute fan_in score (normalized 0-1)
        fan_in_score = min(1.0, metrics.fan_in / self.fan_in_normalization)

        # Classify boundary type
        boundary_type = self.classifier.classify_by_path_only(file_path)

        # Compute boundary score (0.5 if boundary, 0.0 if not)
        boundary_score = 0.5 if boundary_type else 0.0

        # Combine scores: 60% structural, 40% boundary
        combined_score = (0.6 * fan_in_score) + (0.4 * boundary_score)

        score = BlastRadiusScore(
            symbol_id=file_path,
            file_path=file_path,
            fan_in=metrics.fan_in,
            fan_out=metrics.fan_out,
            boundary_type=boundary_type,
            fan_in_score=fan_in_score,
            boundary_score=boundary_score,
            score=combined_score,
        )

        self._score_cache[file_path] = score
        return score

    def rank_files_by_blast_radius(
        self, file_paths: list[str]
    ) -> list[tuple[str, float]]:
        """Rank files by blast radius (highest first).

        Parameters
        ----------
        file_paths:
            List of file paths to rank.

        Returns
        -------
        list[tuple[str, float]]:
            Files sorted by blast radius score (descending).
        """
        scores = [self.compute_score(path) for path in file_paths]
        sorted_scores = sorted(scores, key=lambda s: s.score, reverse=True)
        return [(score.file_path, score.score) for score in sorted_scores]

    def get_top_n_files(self, file_paths: list[str], n: int = 10) -> list[BlastRadiusScore]:
        """Get the top N files by blast radius.

        Parameters
        ----------
        file_paths:
            List of file paths to consider.
        n:
            Number of top files to return.

        Returns
        -------
        list[BlastRadiusScore]:
            Top N files with their scores.
        """
        scores = [self.compute_score(path) for path in file_paths]
        return sorted(scores, key=lambda s: s.score, reverse=True)[:n]

    def get_high_impact_files(
        self, file_paths: list[str], threshold: float = 0.5
    ) -> list[BlastRadiusScore]:
        """Get files above a blast radius threshold.

        Parameters
        ----------
        file_paths:
            List of file paths to consider.
        threshold:
            Score threshold (0.0-1.0).

        Returns
        -------
        list[BlastRadiusScore]:
            Files with score >= threshold, sorted descending.
        """
        scores = [self.compute_score(path) for path in file_paths if self.compute_score(path).score >= threshold]
        return sorted(scores, key=lambda s: s.score, reverse=True)

    def clear_cache(self) -> None:
        """Clear the score cache."""
        self._score_cache.clear()

    def get_score_breakdown(self, file_path: str) -> dict[str, Any]:
        """Get detailed score breakdown for a file.

        Parameters
        ----------
        file_path:
            The file to analyze.

        Returns
        -------
        dict:
            Breakdown of score components.
        """
        score = self.compute_score(file_path)
        return {
            "file_path": score.file_path,
            "total_score": score.score,
            "structural_contribution": score.fan_in_score * 0.6,
            "boundary_contribution": score.boundary_score * 0.4,
            "fan_in": score.fan_in,
            "fan_out": score.fan_out,
            "boundary_type": score.boundary_type,
            "fan_in_normalized": score.fan_in_score,
            "boundary_score": score.boundary_score,
        }

    def explain_score(self, file_path: str) -> str:
        """Return a human-readable explanation of a file's blast radius score.

        Parameters
        ----------
        file_path:
            The file to explain.

        Returns
        -------
        str:
            Human-readable explanation.
        """
        score = self.compute_score(file_path)
        lines = [f"Blast Radius Score: {score.score:.2f} ({score.file_path})"]

        # Structural importance
        if score.fan_in == 0:
            lines.append("  • Structural: No incoming dependencies (low impact)")
        elif score.fan_in <= 5:
            lines.append(f"  • Structural: {score.fan_in} modules depend on this (low-medium impact)")
        elif score.fan_in <= 15:
            lines.append(f"  • Structural: {score.fan_in} modules depend on this (medium-high impact)")
        else:
            lines.append(f"  • Structural: {score.fan_in} modules depend on this (HIGH impact)")

        # Boundary criticality
        if score.boundary_type:
            lines.append(f"  • Boundary: {score.boundary_type.value} boundary (CRITICAL)")
        else:
            lines.append("  • Boundary: Not at critical boundary")

        # Recommendation
        if score.score >= 0.8:
            lines.append("  → HIGH blast radius: Include with full detail in context")
        elif score.score >= 0.5:
            lines.append("  → MEDIUM blast radius: Include with moderate detail")
        elif score.score >= 0.2:
            lines.append("  → LOW blast radius: Brief mention only")
        else:
            lines.append("  → MINIMAL blast radius: May exclude from context")

        return "\n".join(lines)


def rank_repository_files(
    root_path: str,
    file_paths: list[str],
    fan_in_normalization: int = 20,
) -> list[tuple[str, float, BoundaryType | None]]:
    """Convenience function to rank files in a repository.

    Parameters
    ----------
    root_path:
        Root path for the repository.
    file_paths:
        List of file paths to rank.
    fan_in_normalization:
        Fan-in value for normalization (default 20).

    Returns
    -------
    list[tuple[str, float, BoundaryType | None]]:
        Files ranked by blast radius with their boundary types.
    """
    from pathlib import Path

    graph = ImportGraphBuilder()
    graph.build_from_directory(Path(root_path))

    classifier = BoundaryClassifier()

    estimator = BlastRadiusEstimator(graph, classifier, fan_in_normalization)

    ranked = estimator.rank_files_by_blast_radius(file_paths)
    result = []
    for file_path, score in ranked:
        score_obj = estimator.compute_score(file_path)
        result.append((file_path, score, score_obj.boundary_type))

    return result
