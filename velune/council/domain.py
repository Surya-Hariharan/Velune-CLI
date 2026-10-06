"""Vocabulary of the Council core: the enums every other module shares.

Nothing here knows about providers, models, files, permissions or terminals. The
Council is a *reasoning* layer; whether anything it concludes is ever executed is
decided elsewhere (Manual / Plan / Auto), never in this package.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from velune._compat import StrEnum


class CouncilDomain(StrEnum):
    """What kind of deliberation a profile is for. Data only: no module branches on it."""

    GENERAL = "general"
    CODING = "coding"
    TEACHING = "teaching"
    BRAINSTORMING = "brainstorming"


class SeatKind(StrEnum):
    """Who a seat is. Moderator, arbitrator and synthesizer are orchestration roles, not perspectives."""

    PERSPECTIVE = "perspective"
    MODERATOR = "moderator"
    ARBITRATOR = "arbitrator"
    SYNTHESIZER = "synthesizer"


class RoutingRole(StrEnum):
    """Names of the existing model-routing slots a seat borrows (see the runtime adapter).

    Mirrors the values of ``velune.models.specializations.CouncilRole`` without importing
    it, so the core stays free of the provider stack. The adapter tests assert the two
    stay in step. A stopgap until seats are routed by capability.
    """

    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    CHALLENGER = "challenger"
    SYNTHESIZER = "synthesizer"


class StageId(StrEnum):
    """The six stages of the deliberation protocol, R0-R5, in order."""

    FRAME = "frame"
    PERSPECTIVES = "perspectives"
    REVIEW = "review"
    REVISION = "revision"
    ARBITRATION = "arbitration"
    SYNTHESIS = "synthesis"

    @property
    def round(self) -> int:
        return STAGE_ORDER.index(self)


STAGE_ORDER: tuple[StageId, ...] = (
    StageId.FRAME,
    StageId.PERSPECTIVES,
    StageId.REVIEW,
    StageId.REVISION,
    StageId.ARBITRATION,
    StageId.SYNTHESIS,
)


class Depth(StrEnum):
    """How much of the protocol a run uses. There is deliberately no deeper tier yet."""

    QUICK = "quick"
    STANDARD = "standard"


class ArtifactKind(StrEnum):
    """What a stage may read or write."""

    QUESTION = "question"
    EVIDENCE = "evidence"
    FRAME = "frame"
    PERSPECTIVE = "perspective"
    CRITIQUE = "critique"
    REVISION = "revision"
    DECISION = "decision"
    ANSWER = "answer"
    ARTIFACT = "artifact"


class ReadScope(StrEnum):
    """Which artifacts of a kind a seat may see. The independence guarantee is made of these."""

    NONE = "none"
    OWN = "own"  # artifacts the viewing seat authored
    ALL = "all"
    ADDRESSED_TO_SELF = "addressed_to_self"  # artifacts whose target is the viewing seat
    ASSIGNED = "assigned"  # artifacts by authors an AssignmentSource names for the viewer
    DIGEST_ALL = "digest_all"  # a claims-only digest of everyone's artifacts


class Criticality(StrEnum):
    """Whether a failed stage fails the whole run."""

    REQUIRED = "required"
    OPTIONAL = "optional"


class FallbackPolicy(StrEnum):
    """What a stage may do when it cannot run normally (the behaviour itself is a later phase)."""

    NONE = "none"
    DETERMINISTIC_DEFAULT = "deterministic_default"
    SKIP = "skip"


class QuorumRule(BaseModel):
    """How many seats must deliver for a stage to count as having run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_ok: int = Field(ge=0)
    # At least one seat from this set must have delivered (empty = no such requirement).
    require_any_of: tuple[str, ...] = ()
