"""What an agent is: identity, kind, model routing, prompt, visibility, tools and budget.

An :class:`AgentSpec` is pure data. It names a prompt and a routing role, never a provider or a
model, and holds no behaviour. The one rule that matters is enforced here at construction: a
reasoning agent cannot be given tools. Acting agents can be described, but nothing in Phase 4B
executes them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field, StringConstraints, field_validator, model_validator

from velune._compat import StrEnum
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind, ReadScope
from velune.council.profiles import RoutingRole
from velune.council.serialization import Contract

PromptRef = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]

if TYPE_CHECKING:
    from velune.models.specializations import CouncilRole

MAX_TIMEOUT_S = 3600.0
MAX_OUTPUT_TOKENS = 200_000
MAX_INPUT_CHARS = 2_000_000
MAX_TOOLS = 64


class AgentKind(StrEnum):
    """Whether an agent only reasons or may also act through tools."""

    REASONING = "reasoning"
    ACTING = "acting"


class ToolPolicy(Contract):
    """The tools an agent may call. Empty means none, and is the only legal value for reasoning."""

    allowed_tools: tuple[ToolName, ...] = Field(default=(), max_length=MAX_TOOLS)

    @field_validator("allowed_tools")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_tools must not repeat a tool")
        return value

    @property
    def grants_tools(self) -> bool:
        return bool(self.allowed_tools)


class ReadGrant(Contract):
    """One visibility rule: how much of one kind of artifact the agent may see."""

    kind: ArtifactKind
    scope: ReadScope


class AgentBudget(Contract):
    """Hard bounds on a single agent run. Every field is required to be finite and positive."""

    timeout_s: float = Field(default=120.0, gt=0, le=MAX_TIMEOUT_S)
    max_output_tokens: int = Field(default=4096, gt=0, le=MAX_OUTPUT_TOKENS)
    max_input_chars: int = Field(default=200_000, gt=0, le=MAX_INPUT_CHARS)


class AgentSpec(Contract):
    """A frozen, validated description of one agent."""

    role: AgentRole
    kind: AgentKind = AgentKind.REASONING
    # The model-routing slot. ``RoutingRole`` is the council core's provider-free mirror of
    # ``CouncilRole`` (same values, kept in step by a test); ``council_role`` converts.
    routing_role: RoutingRole
    prompt_ref: PromptRef
    read_scope: tuple[ReadGrant, ...] = ()
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    budget: AgentBudget = Field(default_factory=AgentBudget)

    @field_validator("read_scope")
    @classmethod
    def _one_grant_per_kind(cls, value: tuple[ReadGrant, ...]) -> tuple[ReadGrant, ...]:
        kinds = [grant.kind for grant in value]
        if len(set(kinds)) != len(kinds):
            raise ValueError("read_scope must have at most one grant per artifact kind")
        return value

    @model_validator(mode="after")
    def _kind_matches_tools(self) -> AgentSpec:
        if self.kind is AgentKind.REASONING and self.tool_policy.grants_tools:
            raise ValueError(
                "a reasoning agent cannot have tools; make it an acting agent or clear tool_policy"
            )
        if self.kind is AgentKind.ACTING and not self.tool_policy.grants_tools:
            raise ValueError("an acting agent needs at least one allowed tool")
        return self

    @property
    def council_role(self) -> CouncilRole:
        """The real ``CouncilRole`` for model routing. Imported lazily: it loads the provider stack."""
        from velune.models.specializations import CouncilRole

        return CouncilRole(self.routing_role.value)

    def scope_for(self, kind: ArtifactKind) -> ReadScope:
        """The agent's visibility of ``kind``. Anything not granted is ``NONE``."""
        for grant in self.read_scope:
            if grant.kind is kind:
                return grant.scope
        return ReadScope.NONE
