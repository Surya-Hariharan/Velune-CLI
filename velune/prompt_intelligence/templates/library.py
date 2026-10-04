"""Task templates: reusable, provider-independent prompt building blocks.

Role fragments are sourced from :mod:`velune.cognition.prompts` wherever an
equivalent council seat already exists — this registry never re-authors
prompt text that already lives there (the Prompt Intelligence design notes §3.3,
§10). Task kinds with no council-seat equivalent (documentation, research,
terminal execution) fall back to the interactive-chat baseline persona.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from velune.cognition.prompts import (
    CHAT_INTERACTIVE,
    COUNCIL_CODER,
    COUNCIL_PLANNER,
    COUNCIL_REVIEWER,
    get_prompt,
)
from velune.prompt_intelligence.intent import TaskKind
from velune.prompt_intelligence.ir import (
    Constraint,
    OutputContract,
    PlanningStyle,
    WorkflowSpec,
)


@dataclass(frozen=True)
class TaskTemplate:
    """A provider-independent template for one ``TaskKind``."""

    kind: TaskKind
    role_fragment: str
    constraints: tuple[Constraint, ...] = field(default_factory=tuple)
    workflow: WorkflowSpec | None = None
    output_contract: OutputContract | None = None


def _c(text: str) -> Constraint:
    return Constraint(text=text, source="template")


def _default_templates() -> dict[TaskKind, TaskTemplate]:
    return {
        TaskKind.CODING: TaskTemplate(
            kind=TaskKind.CODING,
            role_fragment=get_prompt(COUNCIL_CODER),
            constraints=(
                _c("Make the smallest change that satisfies the request."),
                _c("Match the codebase's existing style and conventions."),
            ),
            workflow=WorkflowSpec(
                style=PlanningStyle.LIGHT_TOUCH,
                steps=("understand", "inspect dependencies", "edit", "verify"),
            ),
        ),
        TaskKind.DEBUGGING: TaskTemplate(
            kind=TaskKind.DEBUGGING,
            role_fragment=get_prompt(COUNCIL_CODER),
            constraints=(
                _c("Identify the root cause before proposing a fix."),
                _c("Do not guess at a fix without inspecting the failing path."),
            ),
            workflow=WorkflowSpec(
                style=PlanningStyle.LIGHT_TOUCH,
                steps=("reproduce", "isolate root cause", "fix", "verify"),
            ),
        ),
        TaskKind.PLANNING: TaskTemplate(
            kind=TaskKind.PLANNING,
            role_fragment=get_prompt(COUNCIL_PLANNER),
            constraints=(_c("Produce a checkable plan with clear done-conditions per step."),),
            workflow=WorkflowSpec(
                style=PlanningStyle.EXPLICIT_PHASES,
                steps=("understand the goal", "decompose into steps", "identify risks", "sequence"),
            ),
        ),
        TaskKind.CODE_REVIEW: TaskTemplate(
            kind=TaskKind.CODE_REVIEW,
            role_fragment=get_prompt(COUNCIL_REVIEWER),
            constraints=(
                _c("Flag correctness and security issues before style issues."),
                _c("Cite the specific file and line for every finding."),
            ),
        ),
        TaskKind.REFACTORING: TaskTemplate(
            kind=TaskKind.REFACTORING,
            role_fragment=get_prompt(COUNCIL_CODER),
            constraints=(
                _c("Preserve existing behavior — a refactor must not change observable output."),
                _c("Prefer many small, reviewable changes over one large rewrite."),
            ),
        ),
        TaskKind.DOCUMENTATION: TaskTemplate(
            kind=TaskKind.DOCUMENTATION,
            role_fragment=get_prompt(CHAT_INTERACTIVE),
            constraints=(_c("Document the why, not the what — code already shows what it does."),),
        ),
        TaskKind.ARCHITECTURE: TaskTemplate(
            kind=TaskKind.ARCHITECTURE,
            role_fragment=get_prompt(COUNCIL_PLANNER),
            constraints=(
                _c("Present options with tradeoffs, not a single unexamined path."),
                _c("State the blast radius of any structural change explicitly."),
            ),
            workflow=WorkflowSpec(
                style=PlanningStyle.EXPLICIT_PHASES,
                steps=(
                    "current-state summary",
                    "options considered",
                    "recommended approach",
                    "blast radius",
                ),
            ),
        ),
        TaskKind.RESEARCH: TaskTemplate(
            kind=TaskKind.RESEARCH,
            role_fragment=get_prompt(CHAT_INTERACTIVE),
            constraints=(_c("Ground claims in what was actually found, not assumption."),),
        ),
        TaskKind.TERMINAL_EXECUTION: TaskTemplate(
            kind=TaskKind.TERMINAL_EXECUTION,
            role_fragment=get_prompt(CHAT_INTERACTIVE),
            constraints=(
                _c(
                    "Explain what a command does before running anything "
                    "destructive or irreversible."
                ),
            ),
        ),
    }


class TaskTemplateRegistry:
    """Maps a ``TaskKind`` to its provider-independent ``TaskTemplate``."""

    def __init__(self) -> None:
        self._templates: dict[TaskKind, TaskTemplate] = _default_templates()

    def get(self, kind: TaskKind) -> TaskTemplate:
        return self._templates[kind]

    def register(self, kind: TaskKind, template: TaskTemplate) -> None:
        """Extensibility hook — add or override a template for one task kind."""
        self._templates[kind] = template
