"""Prompt intermediate representation (IR).

The IR is the only shape task templates, context, and constraints are
expressed in; providers differ only in how a :class:`~velune.prompt_intelligence.
renderers.base.ProviderRenderer` reads it, never in what it contains. See
the Prompt Intelligence design notes §3.1, §7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from velune._compat import StrEnum
from velune.context.sections import ContextSection


class DelimiterStyle(StrEnum):
    """How a renderer marks section boundaries in the final prompt text."""

    XML = "xml"
    MARKDOWN = "markdown"
    PLAIN = "plain"


class ContextOrdering(StrEnum):
    """Where the assembled context block sits relative to role/constraints."""

    STABLE_FIRST = "stable_first"  # role/constraints, then context (Claude)
    CONTEXT_FIRST = "context_first"  # context, then anchor, then instructions (Gemini)
    ROLE_TASK_CONTEXT = "role_task_context"  # role->objectives->constraints->...->context (OpenAI)


class PlanningStyle(StrEnum):
    """How much workflow/plan scaffolding a renderer inserts."""

    EXPLICIT_PHASES = "explicit_phases"
    LIGHT_TOUCH = "light_touch"
    NONE = "none"


@dataclass(frozen=True)
class IRContextNode:
    """One block of assembled context, tagged with its trust and origin.

    ``is_instruction_bearing`` must be ``False`` for anything derived from
    the repository, memory, or a user-authored convention file — dressing
    untrusted data in instruction-shaped delimiters (an XML ``<constraints>``
    tag, a markdown ``## Constraints`` header) must never grant it the
    authority of an actual instruction. See §16.
    """

    section: ContextSection
    content: str
    trust_score: float = 1.0
    is_instruction_bearing: bool = False

    def __post_init__(self) -> None:
        if not (0.0 <= self.trust_score <= 1.0):
            raise ValueError(f"trust_score must be 0.0-1.0, got {self.trust_score}")


@dataclass(frozen=True)
class Constraint:
    """A single instruction-level rule and where it came from.

    ``source`` drives the precedence rule in §6/§14: template constraints
    outrank explicit user-request constraints, which outrank provider
    defaults, whenever two constraints conflict.
    """

    text: str
    source: str  # "template" | "user_request" | "provider_default"


@dataclass(frozen=True)
class WorkflowSpec:
    """Optional plan/execute scaffolding, rendered per ``PlanningStyle``."""

    style: PlanningStyle
    steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutputContract:
    """A description of the shape the model's reply should take."""

    description: str


@dataclass(frozen=True)
class Example:
    """A single few-shot input/output pair."""

    input: str
    output: str


@dataclass
class PromptIR:
    """The provider-agnostic prompt, before any renderer has touched it."""

    role: str
    current_request: str
    objectives: list[str] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    context_nodes: list[IRContextNode] = field(default_factory=list)
    tool_narrative: str | None = None
    workflow: WorkflowSpec | None = None
    output_contract: OutputContract | None = None
    examples: list[Example] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.role or not self.role.strip():
            raise ValueError("PromptIR.role must be non-empty — never compile an empty role (§14)")
        if not self.current_request or not self.current_request.strip():
            raise ValueError("PromptIR.current_request must be non-empty")


@dataclass(frozen=True)
class ProviderProfile:
    """Declarative capability/preference descriptor for one model family.

    Data, not text — see §16: profiles are static, reviewed, checked-in code,
    never constructed from user input or network responses.
    """

    family: str  # a velune.models.family.ModelFamily value, kept as str to avoid an import cycle
    delimiter_style: DelimiterStyle
    context_ordering: ContextOrdering
    planning_style: PlanningStyle
    max_system_tokens: int | None
    supports_prompt_caching: bool
    anchor_phrase: str | None = None


@dataclass(frozen=True)
class RenderedPrompt:
    """The final wire-shape output of a renderer."""

    messages: list[dict[str, Any]]
    cache_hints: dict[int, str] | None = None


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str  # "error" | "warning"


@dataclass(frozen=True)
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


@dataclass(frozen=True)
class CompilationReport:
    """Travels alongside the rendered prompt — never silently discarded."""

    template_used: str
    family: str
    renderer: str
    validation: ValidationReport
    budget_exceeded: bool
    ir_hash: str


@dataclass(frozen=True)
class CompiledPrompt:
    rendered: RenderedPrompt
    report: CompilationReport
