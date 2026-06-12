"""Debug mode utilities for detailed diagnostics and timing.

When --debug flag is used:
- Log level set to DEBUG
- Timing information for each pipeline stage
- Token counts per context section
- Retrieval path and routing decisions with scores
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

logger = structlog.get_logger()


class DebugTimer:
    """Context manager for timing operations with structured logging."""

    def __init__(self, operation_name: str, **context: Any) -> None:
        """Initialize timer.

        Args:
            operation_name: Name of operation to time
            **context: Additional context to log
        """
        self.operation_name = operation_name
        self.context = context
        self.start_time: float | None = None
        self.elapsed_ms: float | None = None

    def __enter__(self) -> DebugTimer:
        """Enter timing context."""
        self.start_time = time.perf_counter()
        logger.debug(
            f"[TIMING] {self.operation_name} started",
            operation=self.operation_name,
            **self.context,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit timing context and log elapsed time."""
        self.elapsed_ms = (time.perf_counter() - self.start_time) * 1000

        if exc_type is None:
            logger.debug(
                f"[TIMING] {self.operation_name} completed",
                operation=self.operation_name,
                elapsed_ms=self.elapsed_ms,
                **self.context,
            )
        else:
            logger.error(
                f"[TIMING] {self.operation_name} failed",
                operation=self.operation_name,
                elapsed_ms=self.elapsed_ms,
                error=str(exc_val),
                **self.context,
            )


@contextmanager
def debug_timer(operation_name: str, **context: Any) -> Iterator[DebugTimer]:
    """Context manager for timing operations.

    Example:
        with debug_timer("context_assembly", workspace="/path"):
            # Assembly code here
    """
    timer = DebugTimer(operation_name, **context)
    with timer:
        yield timer


class TokenCounter:
    """Tracks and logs token usage per context section."""

    def __init__(self) -> None:
        """Initialize counter."""
        self.sections: dict[str, int] = {}
        self.total: int = 0

    def add_section(self, section_name: str, token_count: int) -> None:
        """Record tokens for a section.

        Args:
            section_name: Name of context section (e.g., "repository_files", "memory")
            token_count: Approximate token count for this section
        """
        self.sections[section_name] = token_count
        self.total += token_count

        logger.debug(
            "[TOKEN COUNT] Section added",
            section=section_name,
            tokens=token_count,
            total_so_far=self.total,
        )

    def log_summary(self) -> None:
        """Log a summary of token usage."""
        logger.debug("[TOKEN SUMMARY]")
        for section, count in sorted(self.sections.items(), key=lambda x: -x[1]):
            percentage = (count / self.total * 100) if self.total > 0 else 0
            logger.debug(
                f"  {section}: {count:,} tokens ({percentage:.1f}%)",
                section=section,
                tokens=count,
                percentage=percentage,
            )
        logger.debug(f"  TOTAL: {self.total:,} tokens", total=self.total)

    def to_dict(self) -> dict[str, Any]:
        """Return as dictionary for structured logging."""
        return {
            "sections": self.sections,
            "total_tokens": self.total,
        }


class RoutingDecision:
    """Tracks routing decision with scores."""

    def __init__(self, task_description: str) -> None:
        """Initialize routing decision tracker.

        Args:
            task_description: Description of the task being routed
        """
        self.task_description = task_description
        self.candidates: list[dict[str, Any]] = []
        self.selected: str | None = None
        self.selected_score: float | None = None

    def add_candidate(
        self,
        path_name: str,
        score: float,
        reasoning: str = "",
        **attributes: Any,
    ) -> None:
        """Add a routing candidate with its score.

        Args:
            path_name: Name of routing path (e.g., "fast_path", "full_council")
            score: Score indicating suitability (0-1)
            reasoning: Explanation of score
            **attributes: Additional attributes
        """
        self.candidates.append(
            {
                "path": path_name,
                "score": score,
                "reasoning": reasoning,
                **attributes,
            }
        )

        logger.debug(
            "[ROUTING] Candidate evaluated",
            path=path_name,
            score=score,
            reasoning=reasoning,
        )

    def select(self, path_name: str, score: float) -> None:
        """Record the selected routing path.

        Args:
            path_name: Selected routing path
            score: Final score that drove the selection
        """
        self.selected = path_name
        self.selected_score = score

        logger.info(
            "[ROUTING] Path selected",
            selected_path=path_name,
            score=score,
            candidates_evaluated=len(self.candidates),
        )

    def log_summary(self) -> None:
        """Log detailed routing summary."""
        logger.debug("[ROUTING SUMMARY]")
        logger.debug(f"  Task: {self.task_description}")

        # Sort candidates by score
        sorted_candidates = sorted(self.candidates, key=lambda x: -x["score"])

        for i, candidate in enumerate(sorted_candidates, 1):
            marker = "→ SELECTED" if candidate["path"] == self.selected else ""
            logger.debug(
                f"  {i}. {candidate['path']}: {candidate['score']:.2f} {marker}",
                path=candidate["path"],
                score=candidate["score"],
                selected=candidate["path"] == self.selected,
            )
            if candidate.get("reasoning"):
                logger.debug(f"     Reason: {candidate['reasoning']}")

    def to_dict(self) -> dict[str, Any]:
        """Return as dictionary for structured logging."""
        return {
            "task": self.task_description,
            "selected_path": self.selected,
            "selected_score": self.selected_score,
            "candidates_evaluated": len(self.candidates),
            "all_candidates": self.candidates,
        }


class PipelineMetrics:
    """Tracks metrics for entire pipeline execution."""

    def __init__(self, task_name: str) -> None:
        """Initialize pipeline metrics.

        Args:
            task_name: Name of the pipeline task
        """
        self.task_name = task_name
        self.stages: dict[str, dict[str, Any]] = {}
        self.start_time = time.perf_counter()

    def record_stage(
        self,
        stage_name: str,
        duration_ms: float,
        **details: Any,
    ) -> None:
        """Record a pipeline stage.

        Args:
            stage_name: Name of the stage (e.g., "context_assembly")
            duration_ms: Duration in milliseconds
            **details: Additional metrics (tokens, paths, etc.)
        """
        self.stages[stage_name] = {
            "duration_ms": duration_ms,
            **details,
        }

        logger.debug(
            f"[PIPELINE] {stage_name}",
            stage=stage_name,
            duration_ms=duration_ms,
            **details,
        )

    def log_summary(self) -> None:
        """Log pipeline summary."""
        total_ms = (time.perf_counter() - self.start_time) * 1000

        logger.info("[PIPELINE SUMMARY]")
        logger.info(f"  Task: {self.task_name}")
        logger.info(f"  Total Time: {total_ms:.0f}ms")

        stage_times = sum(s.get("duration_ms", 0) for s in self.stages.values())
        logger.info(f"  Stage Times: {stage_times:.0f}ms")

        for stage_name, metrics in sorted(
            self.stages.items(),
            key=lambda x: -x[1].get("duration_ms", 0),
        ):
            duration = metrics.get("duration_ms", 0)
            pct = (duration / total_ms * 100) if total_ms > 0 else 0
            logger.info(f"  {stage_name}: {duration:.0f}ms ({pct:.1f}%)")

            # Log additional metrics if present
            for key, value in metrics.items():
                if key != "duration_ms":
                    logger.info(f"    {key}: {value}")

    def to_dict(self) -> dict[str, Any]:
        """Return as dictionary for structured logging."""
        return {
            "task": self.task_name,
            "total_ms": (time.perf_counter() - self.start_time) * 1000,
            "stages": self.stages,
        }


def log_debug_info(
    section: str,
    **info: Any,
) -> None:
    """Log debug information.

    Args:
        section: Debug section name (e.g., "context_assembly", "routing")
        **info: Debug details to log
    """
    logger.debug(
        f"[DEBUG] {section}",
        section=section,
        **info,
    )
