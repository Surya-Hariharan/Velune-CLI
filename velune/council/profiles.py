"""Role profiles: which seats a Council has, as validated data.

A profile names the perspective seats of one domain plus the three orchestration seats
(moderator, arbitrator, synthesizer). The protocol, contracts and runner are identical for
every profile; only this data differs. Seat objectives are short descriptive metadata, not
prompts: no prompt text exists in this package.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from velune._compat import StrEnum
from velune.council.domain import CouncilDomain, QuorumRule, SeatKind

_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PREFIX = re.compile(r"^[A-Z]{2,3}$")


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


MIN_PERSPECTIVES = 3
MAX_PERSPECTIVES = 7


class SeatSpec(BaseModel):
    """One seat's identity and the metadata the engine needs about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: SeatKind
    display_name: str = Field(min_length=1, max_length=40)
    objective: str = Field(min_length=1, max_length=200)
    claim_prefix: str  # prefix of the ids of the claims this seat authors, e.g. "AN"
    routing_role: RoutingRole
    # Lookup key into the prompt library (never prompt text itself). ``None`` where no stage
    # prompts that seat yet.
    prompt_key: str | None = None

    @model_validator(mode="after")
    def _check(self) -> SeatSpec:
        if not _SLUG.match(self.id):
            raise ValueError(f"seat id {self.id!r} must be a lowercase slug")
        if not _PREFIX.match(self.claim_prefix):
            raise ValueError(f"claim prefix {self.claim_prefix!r} must be 2-3 capital letters")
        return self


class ReviewAssignment(BaseModel):
    """Who one seat reviews in R2: peers read in full, and peers audited through a claims digest.

    Data, not behaviour. The stages and the assignment source only read it, so a profile for
    another domain brings its own graph.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer: str
    full: tuple[str, ...] = ()
    digest: tuple[str, ...] = ()

    @property
    def targets(self) -> tuple[str, ...]:
        return self.full + self.digest


class RoleProfile(BaseModel):
    """A domain's seats. Perspective seats and orchestration seats are separate fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    domain: CouncilDomain
    version: int = Field(default=1, ge=1)
    perspective_seats: tuple[SeatSpec, ...]
    moderator: SeatSpec
    arbitrator: SeatSpec
    synthesizer: SeatSpec
    problem_types: tuple[str, ...] = ("other",)
    runnable: bool = False
    default_quorum: QuorumRule
    review_graph: tuple[ReviewAssignment, ...] = ()

    def review_for(self, seat_id: str) -> ReviewAssignment | None:
        """What ``seat_id`` reviews in R2, or ``None`` if it reviews nobody."""
        for item in self.review_graph:
            if item.reviewer == seat_id:
                return item
        return None

    @property
    def orchestration_seats(self) -> tuple[SeatSpec, ...]:
        return (self.moderator, self.arbitrator, self.synthesizer)

    @property
    def all_seats(self) -> tuple[SeatSpec, ...]:
        return self.perspective_seats + self.orchestration_seats

    def seat(self, seat_id: str) -> SeatSpec:
        for spec in self.all_seats:
            if spec.id == seat_id:
                return spec
        raise KeyError(f"profile {self.id!r} has no seat {seat_id!r}")

    @model_validator(mode="after")
    def _check(self) -> RoleProfile:
        if not _SLUG.match(self.id):
            raise ValueError(f"profile id {self.id!r} must be a lowercase slug")
        if not MIN_PERSPECTIVES <= len(self.perspective_seats) <= MAX_PERSPECTIVES:
            raise ValueError(
                f"a profile needs {MIN_PERSPECTIVES}-{MAX_PERSPECTIVES} perspective seats, "
                f"got {len(self.perspective_seats)}"
            )
        for spec in self.perspective_seats:
            if spec.kind is not SeatKind.PERSPECTIVE:
                raise ValueError(
                    f"seat {spec.id!r} in perspective_seats has kind {spec.kind.value}"
                )
        for spec, kind in (
            (self.moderator, SeatKind.MODERATOR),
            (self.arbitrator, SeatKind.ARBITRATOR),
            (self.synthesizer, SeatKind.SYNTHESIZER),
        ):
            if spec.kind is not kind:
                raise ValueError(
                    f"seat {spec.id!r} must have kind {kind.value}, not {spec.kind.value}"
                )
        ids = [spec.id for spec in self.all_seats]
        if len(set(ids)) != len(ids):
            raise ValueError("seat ids must be unique within a profile")
        prefixes = [spec.claim_prefix for spec in self.all_seats]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("claim prefixes must be unique within a profile")
        perspective_ids = {spec.id for spec in self.perspective_seats}
        if not set(self.default_quorum.require_any_of) <= perspective_ids:
            raise ValueError("quorum require_any_of must name perspective seats")
        if self.default_quorum.min_ok > len(self.perspective_seats):
            raise ValueError("quorum min_ok exceeds the number of perspective seats")
        reviewers: set[str] = set()
        for item in self.review_graph:
            if item.reviewer not in perspective_ids:
                raise ValueError(
                    f"review graph reviewer {item.reviewer!r} is not a perspective seat"
                )
            if item.reviewer in reviewers:
                raise ValueError(f"review graph lists reviewer {item.reviewer!r} twice")
            reviewers.add(item.reviewer)
            if len(set(item.targets)) != len(item.targets):
                raise ValueError(f"{item.reviewer!r} reviews a seat more than once")
            if item.reviewer in item.targets:
                raise ValueError(f"{item.reviewer!r} cannot review itself")
            if not set(item.targets) <= perspective_ids:
                raise ValueError(f"{item.reviewer!r} reviews a seat that is not a perspective seat")
        return self


class UnknownProfile(KeyError):
    """No profile with that id is registered."""


class ProfileNotRunnable(Exception):
    """The profile exists but is declared data only (no stages can run it yet)."""


class ProfileRegistry:
    """Registered profiles by id. Built fresh; there is no global mutable registry."""

    def __init__(self, profiles: tuple[RoleProfile, ...] = ()) -> None:
        self._profiles: dict[str, RoleProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: RoleProfile) -> None:
        if profile.id in self._profiles:
            raise ValueError(f"profile {profile.id!r} is already registered")
        self._profiles[profile.id] = profile

    def get(self, profile_id: str) -> RoleProfile:
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise UnknownProfile(profile_id) from None

    def require_runnable(self, profile_id: str) -> RoleProfile:
        profile = self.get(profile_id)
        if not profile.runnable:
            raise ProfileNotRunnable(f"profile {profile_id!r} is declared but not runnable yet")
        return profile

    def ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)


# ── orchestration seats (shared by every profile) ────────────────────────────

MODERATOR = SeatSpec(
    id="moderator",
    kind=SeatKind.MODERATOR,
    display_name="Moderator",
    objective="Scope the question neutrally without answering it",
    claim_prefix="MO",
    routing_role=RoutingRole.PLANNER,
)
ARBITRATOR = SeatSpec(
    id="arbitrator",
    kind=SeatKind.ARBITRATOR,
    display_name="Arbitrator",
    objective="Decide which claims survived deliberation on evidence, not votes",
    claim_prefix="AR",
    routing_role=RoutingRole.REVIEWER,
)
SYNTHESIZER = SeatSpec(
    id="synthesizer",
    kind=SeatKind.SYNTHESIZER,
    display_name="Synthesizer",
    objective="Report what the council concluded as one faithful answer",
    claim_prefix="SY",
    routing_role=RoutingRole.SYNTHESIZER,
)


GENERAL_PROMPT_NAMESPACE = "council.general"


def _with_prompt_keys(profile: RoleProfile, namespace: str) -> RoleProfile:
    """Point the seats a stage actually prompts (perspectives and moderator) at the prompt library."""

    def keyed(spec: SeatSpec) -> SeatSpec:
        return spec.model_copy(update={"prompt_key": f"{namespace}.{spec.id}"})

    return profile.model_copy(
        update={
            "perspective_seats": tuple(keyed(s) for s in profile.perspective_seats),
            "moderator": keyed(profile.moderator),
        }
    )


def _seat(seat_id: str, name: str, prefix: str, role: RoutingRole, objective: str) -> SeatSpec:
    return SeatSpec(
        id=seat_id,
        kind=SeatKind.PERSPECTIVE,
        display_name=name,
        objective=objective,
        claim_prefix=prefix,
        routing_role=role,
    )


# ── GENERAL: the first domain, runnable once stages exist ───────────────────

_GENERAL_DATA = RoleProfile(
    id="general",
    domain=CouncilDomain.GENERAL,
    perspective_seats=(
        _seat(
            "analyst",
            "Analyst",
            "AN",
            RoutingRole.REVIEWER,
            "Build the strongest analytical understanding and commit to a position",
        ),
        _seat(
            "skeptic",
            "Skeptic",
            "SK",
            RoutingRole.CHALLENGER,
            "Find where the obvious answer could be wrong or incomplete",
        ),
        _seat(
            "creative",
            "Creative",
            "CR",
            RoutingRole.PLANNER,
            "Widen the space of possible answers",
        ),
        _seat(
            "fact_checker",
            "Fact Checker",
            "FC",
            RoutingRole.REVIEWER,
            "Evaluate factual footing and the quality of evidence",
        ),
        _seat(
            "practicalist",
            "Practicalist",
            "PR",
            RoutingRole.PLANNER,
            "Judge feasibility, cost and real-world consequences",
        ),
    ),
    moderator=MODERATOR,
    arbitrator=ARBITRATOR,
    synthesizer=SYNTHESIZER,
    problem_types=(
        "explanation",
        "comparison",
        "decision",
        "brainstorm",
        "planning",
        "debate",
        "factual",
        "creative",
        "diagnostic",
        "other",
    ),
    runnable=True,
    default_quorum=QuorumRule(min_ok=3, require_any_of=("skeptic", "fact_checker")),
    # Structured selective review: four seats read two role-opposed peers in full and the Fact
    # Checker audits all four through a claims digest. Analyst and Practicalist therefore lean on
    # the Fact Checker's digest review for their second reviewer.
    review_graph=(
        ReviewAssignment(reviewer="analyst", full=("skeptic", "fact_checker")),
        ReviewAssignment(reviewer="skeptic", full=("analyst", "creative")),
        ReviewAssignment(reviewer="creative", full=("skeptic", "practicalist")),
        ReviewAssignment(
            reviewer="fact_checker", digest=("analyst", "skeptic", "creative", "practicalist")
        ),
        ReviewAssignment(reviewer="practicalist", full=("creative", "fact_checker")),
    ),
)

GENERAL_PROFILE = _with_prompt_keys(_GENERAL_DATA, GENERAL_PROMPT_NAMESPACE)

# ── declared domains: validated data only, not runnable ──────────────────────
# Seat names are provisional; their job here is to prove the engine has no domain branches.

CODING_PROFILE = RoleProfile(
    id="coding",
    domain=CouncilDomain.CODING,
    perspective_seats=(
        _seat(
            "architect", "Architect", "AC", RoutingRole.PLANNER, "Judge structure and boundaries"
        ),
        _seat("implementer", "Implementer", "IM", RoutingRole.CODER, "Work out how to build it"),
        _seat("reviewer", "Reviewer", "RV", RoutingRole.REVIEWER, "Audit correctness and risk"),
        _seat(
            "security_analyst",
            "Security Analyst",
            "SA",
            RoutingRole.CHALLENGER,
            "Look for ways it can be abused",
        ),
        _seat("tester", "Tester", "TS", RoutingRole.REVIEWER, "Decide how it will be verified"),
    ),
    moderator=MODERATOR,
    arbitrator=ARBITRATOR,
    synthesizer=SYNTHESIZER,
    problem_types=("implementation", "debugging", "refactoring", "review", "design", "other"),
    runnable=False,
    default_quorum=QuorumRule(min_ok=3, require_any_of=("reviewer", "security_analyst")),
)

TEACHING_PROFILE = RoleProfile(
    id="teaching",
    domain=CouncilDomain.TEACHING,
    perspective_seats=(
        _seat("teacher", "Teacher", "TC", RoutingRole.PLANNER, "Explain the idea clearly"),
        _seat(
            "socratic_tutor",
            "Socratic Tutor",
            "ST",
            RoutingRole.CHALLENGER,
            "Ask the questions that lead the learner there",
        ),
        _seat("examiner", "Examiner", "EX", RoutingRole.REVIEWER, "Test whether it was understood"),
        _seat(
            "misconception_detector",
            "Misconception Detector",
            "MD",
            RoutingRole.REVIEWER,
            "Anticipate where learners go wrong",
        ),
        _seat(
            "example_generator",
            "Example Generator",
            "EG",
            RoutingRole.PLANNER,
            "Produce examples that make it concrete",
        ),
    ),
    moderator=MODERATOR,
    arbitrator=ARBITRATOR,
    synthesizer=SYNTHESIZER,
    problem_types=("explain", "practice", "assess", "other"),
    runnable=False,
    default_quorum=QuorumRule(min_ok=3, require_any_of=("examiner", "misconception_detector")),
)

BRAINSTORMING_PROFILE = RoleProfile(
    id="brainstorming",
    domain=CouncilDomain.BRAINSTORMING,
    perspective_seats=(
        _seat(
            "divergent_thinker",
            "Divergent Thinker",
            "DT",
            RoutingRole.PLANNER,
            "Generate many different directions",
        ),
        _seat(
            "analogist", "Analogist", "AL", RoutingRole.PLANNER, "Borrow ideas from other fields"
        ),
        _seat("contrarian", "Contrarian", "CT", RoutingRole.CHALLENGER, "Invert the obvious idea"),
        _seat(
            "feasibility_checker",
            "Feasibility Checker",
            "FE",
            RoutingRole.REVIEWER,
            "Test which ideas could really work",
        ),
        _seat(
            "curator", "Curator", "CU", RoutingRole.REVIEWER, "Pick and group the strongest ideas"
        ),
    ),
    moderator=MODERATOR,
    arbitrator=ARBITRATOR,
    synthesizer=SYNTHESIZER,
    problem_types=("ideation", "naming", "strategy", "other"),
    runnable=False,
    default_quorum=QuorumRule(min_ok=3, require_any_of=("feasibility_checker",)),
)

BUILTIN_PROFILES: tuple[RoleProfile, ...] = (
    GENERAL_PROFILE,
    CODING_PROFILE,
    TEACHING_PROFILE,
    BRAINSTORMING_PROFILE,
)


def default_registry() -> ProfileRegistry:
    """A fresh registry holding the built-in profiles."""
    return ProfileRegistry(BUILTIN_PROFILES)
