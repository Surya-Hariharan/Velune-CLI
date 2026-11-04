"""Native multi-agent orchestration engine: the Phase 4B foundation.

Typed agent specs, an explicit registry, an immutable run state with a pure state machine, a
reasoning-only runner over a small invoker port, and (Phase 4C) a bounded plan-and-review driver. Nothing here is wired to the CLI, the REPL or the
legacy council, and the ``orchestration.engine`` flag defaults to ``"off"``. The legacy council
(``velune.cognition``) and the deliberative core (``velune.council``) are untouched.
"""

from velune.orchestration.engine.artifacts import (
    ArtifactOrigin,
    ArtifactReader,
    ArtifactStore,
    AssignmentPolicy,
    ContextBuilder,
    StaticAssignments,
    StoredArtifact,
)
from velune.orchestration.engine.driver import (
    DriverResult,
    PlanReviewDriver,
    RunHandle,
    Verdict,
    parse_verdict,
    plan_review_specs,
)
from velune.orchestration.engine.errors import (
    DriverConfigError,
    DuplicateAgentError,
    EngineError,
    IllegalTransitionError,
    InvalidArtifactError,
    RetryLimitExceeded,
    TerminalStateError,
    UnknownAgentError,
)
from velune.orchestration.engine.flag import ENGINE_OFF, ENGINE_ON, engine_enabled, engine_mode
from velune.orchestration.engine.machine import (
    TRANSITIONS,
    advance,
    allowed_from,
    can_transition,
    cancel,
    fail,
    record_artifact,
)
from velune.orchestration.engine.ports import (
    ActingInvoker,
    ActingRequest,
    ActingResult,
    PromptResolver,
    ReasoningInvoker,
    ReasoningRequest,
    ReasoningResult,
    StaticPromptResolver,
)
from velune.orchestration.engine.proposals import ProposedToolCall
from velune.orchestration.engine.registry import AgentRegistry
from velune.orchestration.engine.runner import (
    AgentFailure,
    AgentInput,
    AgentOutcome,
    AgentRunner,
    ContextItem,
    FailureKind,
    OutcomeStatus,
    ReasoningAgentRunner,
)
from velune.orchestration.engine.spec import (
    AgentBudget,
    AgentKind,
    AgentSpec,
    ReadGrant,
    ToolPolicy,
)
from velune.orchestration.engine.toolpolicy import (
    EXCLUDED_TOOLS,
    READ_ONLY_PERMISSIONS,
    READ_ONLY_TOOL_CEILING,
    ResolvedToolPolicy,
    ToolDescriptor,
    ToolPolicyError,
    descriptors_from_registry,
    resolve_tool_policy,
)
from velune.orchestration.engine.toolvalidate import (
    ToolCallRejected,
    ToolRejection,
    ValidatedToolCall,
    validate_proposals,
    validate_tool_call,
)

__all__ = [
    "EXCLUDED_TOOLS",
    "READ_ONLY_PERMISSIONS",
    "READ_ONLY_TOOL_CEILING",
    "ActingInvoker",
    "ActingRequest",
    "ActingResult",
    "ArtifactOrigin",
    "ProposedToolCall",
    "ResolvedToolPolicy",
    "ToolCallRejected",
    "ToolDescriptor",
    "ToolPolicyError",
    "ToolRejection",
    "ValidatedToolCall",
    "descriptors_from_registry",
    "resolve_tool_policy",
    "validate_proposals",
    "validate_tool_call",
    "ENGINE_OFF",
    "ENGINE_ON",
    "TRANSITIONS",
    "AgentBudget",
    "AgentFailure",
    "AgentInput",
    "AgentKind",
    "AgentOutcome",
    "AgentRegistry",
    "AgentRunner",
    "AgentSpec",
    "ArtifactReader",
    "ArtifactStore",
    "AssignmentPolicy",
    "ContextBuilder",
    "ContextItem",
    "DriverConfigError",
    "DriverResult",
    "DuplicateAgentError",
    "EngineError",
    "FailureKind",
    "IllegalTransitionError",
    "InvalidArtifactError",
    "OutcomeStatus",
    "PlanReviewDriver",
    "PromptResolver",
    "ReadGrant",
    "ReasoningAgentRunner",
    "ReasoningInvoker",
    "ReasoningRequest",
    "ReasoningResult",
    "RetryLimitExceeded",
    "RunHandle",
    "StaticAssignments",
    "StaticPromptResolver",
    "StoredArtifact",
    "TerminalStateError",
    "ToolPolicy",
    "UnknownAgentError",
    "Verdict",
    "advance",
    "allowed_from",
    "can_transition",
    "cancel",
    "engine_enabled",
    "engine_mode",
    "fail",
    "parse_verdict",
    "plan_review_specs",
    "record_artifact",
]
