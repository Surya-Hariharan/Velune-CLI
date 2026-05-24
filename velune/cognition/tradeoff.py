"""Trade-off Evaluation Matrix (TEM).

Provides a weighted multi-criteria scoring engine that Velune uses to
objectively evaluate competing architectural alternatives and select the
mathematically optimal solution.  Results are persisted to the lineage
database so future councils can reason about historical trade-off rationale.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("velune.cognition.tradeoff")

# ─────────────────────────────────────────────────────────────────────────────
# Default evaluation axes and their weights (must sum to 1.0).
# Weights can be overridden at construction time.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_AXES: Dict[str, float] = {
    "performance": 0.25,       # CPU / latency / throughput
    "maintainability": 0.25,   # LCOM, coupling, readability
    "safety": 0.20,            # thread / memory / exception safety
    "scalability": 0.15,       # concurrency, horizontal scaling potential
    "simplicity": 0.15,        # implementation complexity, LoC, cognitive load
}


@dataclass
class TemOption:
    """A single architectural alternative registered in the matrix."""

    name: str
    metrics: Dict[str, float]          # axis -> score in [0.0, 1.0]
    notes: str = ""
    weighted_score: float = field(default=0.0, init=False)


class TradeoffEvaluationMatrix:
    """Weighted multi-criteria decision engine for architectural trade-offs.

    Usage::

        tem = TradeoffEvaluationMatrix(task_id="TEM-001")
        tem.add_option("Async Queue", {"performance": 0.9, "safety": 0.8, ...})
        tem.add_option("Thread Lock", {"performance": 0.6, "safety": 0.95, ...})
        winner = tem.select_optimal()
        report  = tem.explain_decision()

    Args:
        task_id: Unique identifier for this trade-off evaluation session.
        axes: Optional dict of axis names to weights (must sum to ~1.0).
        lineage_memory: Optional ``LineageMemoryTier`` instance for persistence.
    """

    def __init__(
        self,
        task_id: str,
        axes: Optional[Dict[str, float]] = None,
        lineage_memory: Optional[Any] = None,
    ) -> None:
        self.task_id = task_id
        self.axes: Dict[str, float] = axes or DEFAULT_AXES
        self.lineage_memory = lineage_memory
        self.options: List[TemOption] = []
        self._evaluated = False

        # Normalise weights so rounding errors do not penalise scores.
        total = sum(self.axes.values())
        if total > 0:
            self.axes = {k: v / total for k, v in self.axes.items()}

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def add_option(
        self,
        name: str,
        metrics: Dict[str, float],
        notes: str = "",
    ) -> "TradeoffEvaluationMatrix":
        """Register a competing architectural alternative.

        Args:
            name: Human-readable label for this alternative.
            metrics: Dict mapping axis names to scores in [0.0, 1.0].  Missing
                axes default to 0.5 (neutral).
            notes: Optional free-form rationale or implementation notes.

        Returns:
            Self, enabling method chaining.
        """
        # Clamp every metric value to [0.0, 1.0]
        clamped = {
            axis: max(0.0, min(1.0, float(metrics.get(axis, 0.5))))
            for axis in self.axes
        }
        opt = TemOption(name=name, metrics=clamped, notes=notes)
        self.options.append(opt)
        self._evaluated = False
        logger.debug("TEM option registered: %s", name)
        return self

    def select_optimal(self) -> TemOption:
        """Fuse weighted scores for each option and return the top-ranked one.

        Returns:
            The ``TemOption`` with the highest weighted composite score.

        Raises:
            ValueError: When no options have been registered.
        """
        if not self.options:
            raise ValueError(
                "TradeoffEvaluationMatrix has no options registered. "
                "Call add_option() before select_optimal()."
            )

        self._score_all()
        winner = max(self.options, key=lambda o: o.weighted_score)
        logger.info(
            "TEM [%s] winner: '%s' (score=%.4f)",
            self.task_id,
            winner.name,
            winner.weighted_score,
        )

        # Persist to lineage DB when available
        self._persist_decision(winner)
        return winner

    def explain_decision(self) -> str:
        """Render a structured Markdown rationale report for the evaluation.

        Returns:
            Multi-line Markdown string summarising scores, rankings, and the
            selection rationale.

        Raises:
            ValueError: When no options have been registered.
        """
        if not self.options:
            raise ValueError("No options to explain. Call add_option() first.")

        if not self._evaluated:
            self._score_all()

        sorted_opts = sorted(self.options, key=lambda o: o.weighted_score, reverse=True)
        winner = sorted_opts[0]

        lines: List[str] = [
            f"# Trade-off Evaluation Report: `{self.task_id}`\n",
            f"**Evaluation Timestamp**: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n",
            "## Evaluation Axes & Weights\n",
        ]

        for axis, weight in self.axes.items():
            lines.append(f"- **{axis.capitalize()}**: weight `{weight:.2f}`")

        lines.append("\n## Option Scorecards\n")
        lines.append("| Rank | Option | " + " | ".join(a.capitalize() for a in self.axes) + " | Weighted Score |")
        lines.append("|------|--------|" + "|".join("---" for _ in self.axes) + "|----------------|")

        for rank, opt in enumerate(sorted_opts, start=1):
            scores = " | ".join(f"{opt.metrics.get(a, 0.0):.2f}" for a in self.axes)
            lines.append(f"| {rank} | **{opt.name}** | {scores} | **{opt.weighted_score:.4f}** |")

        lines.append(f"\n## \u2705 Selected Architecture: `{winner.name}`\n")
        lines.append(
            f"Composite weighted score: **{winner.weighted_score:.4f}** "
            f"({'%.1f%%' % (winner.weighted_score * 100)} of maximum)\n"
        )
        if winner.notes:
            lines.append(f"**Implementation Notes**: {winner.notes}\n")

        if len(sorted_opts) > 1:
            runner_up = sorted_opts[1]
            margin = winner.weighted_score - runner_up.weighted_score
            lines.append(
                f"**Margin over runner-up `{runner_up.name}`**: "
                f"+{margin:.4f} ({margin / max(runner_up.weighted_score, 1e-9) * 100:.1f}% advantage)\n"
            )

        lines.append("\n## Rejected Alternatives\n")
        for opt in sorted_opts[1:]:
            lines.append(
                f"- **{opt.name}** (score: {opt.weighted_score:.4f})"
                + (f" — {opt.notes}" if opt.notes else "")
            )

        return "\n".join(lines)

    def get_rankings(self) -> List[Dict[str, Any]]:
        """Return all options sorted by weighted score, descending.

        Returns:
            List of dicts with keys ``name``, ``weighted_score``, ``metrics``, and ``notes``.
        """
        if not self._evaluated:
            self._score_all()
        sorted_opts = sorted(self.options, key=lambda o: o.weighted_score, reverse=True)
        return [
            {
                "name": o.name,
                "weighted_score": o.weighted_score,
                "metrics": o.metrics,
                "notes": o.notes,
            }
            for o in sorted_opts
        ]

    # ─────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────

    def _score_all(self) -> None:
        """Compute and assign weighted composite scores to all registered options."""
        for opt in self.options:
            opt.weighted_score = sum(
                self.axes.get(axis, 0.0) * opt.metrics.get(axis, 0.0)
                for axis in self.axes
            )
        self._evaluated = True

    def _persist_decision(self, winner: TemOption) -> None:
        """Persist the TEM decision to the lineage memory tier if available."""
        if self.lineage_memory is None:
            return
        try:
            alternatives = [
                {
                    "option_name": o.name,
                    "tradeoffs": o.metrics,
                    "rejected_reason": (
                        ""
                        if o.name == winner.name
                        else f"Lower composite score ({o.weighted_score:.4f} vs {winner.weighted_score:.4f})"
                    ),
                }
                for o in self.options
            ]
            self.lineage_memory.log_decision(
                decision_id=f"TEM-{self.task_id}-{int(time.time())}",
                target_subsystem=self.task_id,
                rationale=f"TEM selected '{winner.name}' with score {winner.weighted_score:.4f}",
                architectural_impact=round(winner.weighted_score, 3),
                consequences=f"Architecture '{winner.name}' approved by Trade-off Evaluation Matrix.",
                alternatives=alternatives,
            )
            logger.info("TEM decision persisted to lineage DB for task '%s'.", self.task_id)
        except Exception as exc:
            logger.warning("Failed to persist TEM decision: %s", exc)
