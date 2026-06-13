"""Orchestration pipeline ribbon — a calm, single-line stage tracker.

Renders a council/orchestration run as a horizontal pipeline so the operator
can see, at a glance, which stage owns the work right now and what has already
completed:

    Planner ✓ → Coder ◆ → Reviewer · → Synthesis ·

This is deliberately *infrastructure-grade*, not decorative: one stable line,
no animation, semantic glyphs/colors only. It complements (does not replace)
the per-phase detail panel by giving the run a persistent spine.

The state machine is intentionally tiny and pure so it can be unit-tested
without a terminal: feed it phase names as milestones arrive, ask it for a
Rich renderable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text

from velune.cli import design

# Canonical council stages in execution order. Streamed phase names are matched
# case-insensitively against these; unknown phases are appended as they appear
# so the ribbon never hides work it doesn't recognize.
_CANONICAL_STAGES: tuple[str, ...] = (
    "planner",
    "coder",
    "reviewer",
    "challenger",
    "arbitration",
    "synthesis",
)

# Stage lifecycle states and their glyph + semantic color.
_GLYPHS: dict[str, tuple[str, str]] = {
    "waiting": ("·", design.FAINT),
    "active": ("◆", design.ACCENT_SOFT),
    "done": ("✓", design.OK),
    "failed": ("✗", design.DANGER),
}


@dataclass
class PipelineTracker:
    """Tracks stage progression for a single orchestration run.

    ``advance(phase)`` is called once per streamed milestone. The first time a
    phase is seen it becomes ``active`` and every earlier stage is marked
    ``done``; ``fail(phase)`` marks the current stage ``failed`` and stops
    auto-completing it. Rendering reads only this state, so a Rich ``Live`` can
    refresh the ribbon cheaply between updates.
    """

    stages: list[str] = field(default_factory=lambda: list(_CANONICAL_STAGES))
    _state: dict[str, str] = field(default_factory=dict)
    _active: str | None = None

    def __post_init__(self) -> None:
        for stage in self.stages:
            self._state.setdefault(stage, "waiting")

    def _normalize(self, phase: str) -> str:
        return (phase or "").strip().lower()

    def advance(self, phase: str) -> None:
        """Mark *phase* active, completing every stage before it."""
        name = self._normalize(phase)
        if not name:
            return
        if name not in self._state:
            # Unknown phase (e.g. "debate", "context reconstruction"): surface it
            # rather than dropping it, appended after the known stages.
            self.stages.append(name)
            self._state[name] = "waiting"

        idx = self.stages.index(name)
        for earlier in self.stages[:idx]:
            if self._state[earlier] not in ("failed",):
                self._state[earlier] = "done"
        if self._state[name] != "failed":
            self._state[name] = "active"
        self._active = name

    def fail(self, phase: str | None = None) -> None:
        """Mark *phase* (or the current active stage) as failed."""
        name = self._normalize(phase) if phase else self._active
        if name and name in self._state:
            self._state[name] = "failed"

    def complete(self) -> None:
        """Mark the run finished: the active stage and all priors become done."""
        for stage in self.stages:
            if self._state[stage] == "active":
                self._state[stage] = "done"
        self._active = None

    def state_of(self, phase: str) -> str:
        return self._state.get(self._normalize(phase), "waiting")

    def render(self) -> Text:
        """Build the single-line Rich renderable for the current state."""
        text = Text(no_wrap=False)
        for i, stage in enumerate(self.stages):
            if i:
                text.append("  →  ", style=design.FAINT)
            glyph, color = _GLYPHS[self._state[stage]]
            label = stage[:1].upper() + stage[1:]
            is_active = self._state[stage] == "active"
            style = f"bold {color}" if is_active else color
            text.append(f"{glyph} ", style=style)
            text.append(label, style=style if is_active else design.MUTED)
        return text
