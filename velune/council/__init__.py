"""Domain-neutral Council core: contracts, profiles, stages and a stage-agnostic runner.

This package is the *reasoning* layer. It cannot execute, edit, ask permission or render
anything: Manual / Plan / Auto (execution authority) live elsewhere and are untouched.
Provider, model and trace access happen only behind the ports in ``velune.council.ports``,
bound to the real runtime by ``velune.council.adapters``, which this module never imports.

``velune.cognition.council`` is the separate, existing coding pipeline.
"""

from __future__ import annotations

from velune.council.contracts import (
    ArbitrationResult,
    Artifact,
    Claim,
    Critique,
    Frame,
    Perspective,
    Revision,
)
from velune.council.domain import (
    ArtifactKind,
    CouncilDomain,
    Criticality,
    Depth,
    QuorumRule,
    ReadScope,
    SeatKind,
    StageId,
)
from velune.council.frame import FrameStage
from velune.council.perspectives import PerspectiveStage
from velune.council.ports import (
    AsyncioScheduler,
    Clock,
    ContentScreen,
    IdSource,
    PromptSource,
    Scheduler,
    SeatCall,
    SeatInvoker,
    TraceSink,
)
from velune.council.profiles import (
    GENERAL_PROFILE,
    ProfileRegistry,
    RoleProfile,
    SeatSpec,
    default_registry,
)
from velune.council.report import frame_of, perspectives_of
from velune.council.request import (
    CouncilRequest,
    CouncilSettings,
    EvidenceItem,
    ResponseRequirements,
)
from velune.council.results import (
    CouncilOutcome,
    OutcomeStatus,
    SeatResult,
    SeatStatus,
    StageResult,
    StageStatus,
)
from velune.council.runner import CouncilCancelled, StagedCouncilRunner
from velune.council.serialization import canonical_json, digest
from velune.council.stages import (
    EXPLORATION_PLAN,
    STAGE_CONTRACTS,
    Stage,
    StageContract,
    StageOutput,
    StagePlan,
    VisibilityPolicy,
)
from velune.council.state import StageContext, StageView, VisibilityViolation

__all__ = [
    "EXPLORATION_PLAN",
    "GENERAL_PROFILE",
    "STAGE_CONTRACTS",
    "ArbitrationResult",
    "Artifact",
    "ArtifactKind",
    "AsyncioScheduler",
    "Claim",
    "Clock",
    "ContentScreen",
    "CouncilCancelled",
    "CouncilDomain",
    "CouncilOutcome",
    "CouncilRequest",
    "CouncilSettings",
    "Criticality",
    "Critique",
    "Depth",
    "EvidenceItem",
    "Frame",
    "FrameStage",
    "IdSource",
    "OutcomeStatus",
    "Perspective",
    "PerspectiveStage",
    "ProfileRegistry",
    "PromptSource",
    "QuorumRule",
    "ReadScope",
    "ResponseRequirements",
    "Revision",
    "RoleProfile",
    "Scheduler",
    "SeatCall",
    "SeatInvoker",
    "SeatKind",
    "SeatResult",
    "SeatSpec",
    "SeatStatus",
    "Stage",
    "StageContext",
    "StageContract",
    "StageId",
    "StageOutput",
    "StagePlan",
    "StageResult",
    "StageStatus",
    "StageView",
    "StagedCouncilRunner",
    "TraceSink",
    "VisibilityPolicy",
    "VisibilityViolation",
    "canonical_json",
    "default_registry",
    "digest",
    "frame_of",
    "perspectives_of",
]
