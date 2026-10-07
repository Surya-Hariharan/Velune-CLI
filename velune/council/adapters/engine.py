"""The explicit-opt-in entry point to the deliberative engine (Phase 2A: frame + perspectives).

Nothing in Velune calls this module. It is reachable only by code that asks for it, and
``DeliberativeEngine.create`` refuses unless ``cognition.council_engine`` is ``"deliberative"``
(default ``"legacy"``; env ``VELUNE_COGNITION__COUNCIL_ENGINE``). ``/council``, ``/run``, ``ask`` and
MCP are unchanged whether the flag is on or off.

An exploration run is evidence, not an answer. It runs only the frame and perspectives stages, so
its outcome has ``answer=None`` even when it succeeds: read the frame and perspectives through
``velune.council.report`` and never present the outcome to a user as an answer. There is no
arbitration or synthesis yet.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from velune.cognition.execution_trace import current_trace, trace_request
from velune.cognition.prompts import deliberation_digest
from velune.council.adapters.prompts import LibraryPrompts
from velune.council.adapters.runtime import (
    FirewallScreen,
    RequestTraceSink,
    RuntimeSeatInvoker,
    SystemClock,
    UuidIds,
)
from velune.council.frame import FrameStage
from velune.council.perspectives import PerspectiveStage
from velune.council.ports import ContentScreen, IdSource, PromptSource, SeatInvoker
from velune.council.profiles import GENERAL_PROFILE, ProfileRegistry, RoleProfile, default_registry
from velune.council.request import (
    CouncilRequest,
    CouncilSettings,
    EvidenceItem,
    ResponseRequirements,
)
from velune.council.results import CouncilOutcome
from velune.council.runner import StagedCouncilRunner
from velune.council.stages import EXPLORATION_PLAN

ENGINE_LEGACY = "legacy"
ENGINE_DELIBERATIVE = "deliberative"

InvokerFactory = Callable[[RoleProfile, str], SeatInvoker]


class EngineDisabled(RuntimeError):
    """The deliberative engine was requested but ``cognition.council_engine`` is not enabled."""


def engine_mode(config: Any) -> str:
    """The configured engine, ``"legacy"`` unless explicitly set to ``"deliberative"``."""
    mode = getattr(getattr(config, "cognition", None), "council_engine", ENGINE_LEGACY)
    return ENGINE_DELIBERATIVE if mode == ENGINE_DELIBERATIVE else ENGINE_LEGACY


def engine_enabled(config: Any) -> bool:
    return engine_mode(config) == ENGINE_DELIBERATIVE


class DeliberativeEngine:
    """Runs frame + perspectives for the GENERAL profile. Construct with :meth:`create`."""

    def __init__(
        self,
        *,
        invoker_factory: InvokerFactory,
        prompts: PromptSource | None = None,
        profile: RoleProfile = GENERAL_PROFILE,
        registry: ProfileRegistry | None = None,
        ids: IdSource | None = None,
        screen: ContentScreen | None = None,
    ) -> None:
        self._invoker_factory = invoker_factory
        self._profile = profile
        self._registry = registry or default_registry()
        self._prompts = prompts or LibraryPrompts(profile)
        self._ids = ids or UuidIds()
        self._screen = screen if screen is not None else FirewallScreen()

    @classmethod
    def create(
        cls,
        container: Any,
        config: Any,
        *,
        invoker_factory: InvokerFactory | None = None,
    ) -> DeliberativeEngine:
        """Build the engine, or raise ``EngineDisabled`` without touching the runtime."""
        if not engine_enabled(config):
            raise EngineDisabled(
                "the deliberative council engine is disabled; set cognition.council_engine = "
                "'deliberative' (or VELUNE_COGNITION__COUNCIL_ENGINE=deliberative) to opt in"
            )
        factory = invoker_factory or (
            lambda profile, run_id: RuntimeSeatInvoker.from_container(container, profile, run_id)
        )
        return cls(invoker_factory=factory)

    def build_request(
        self,
        question: str,
        *,
        context: str = "",
        evidence: Sequence[EvidenceItem] = (),
        response: ResponseRequirements | None = None,
        settings: CouncilSettings | None = None,
    ) -> CouncilRequest:
        return CouncilRequest(
            request_id=self._ids.next_id("req"),
            question=question,
            profile_id=self._profile.id,
            context=context,
            evidence=tuple(evidence),
            response=response or ResponseRequirements(),
            settings=settings or CouncilSettings(),
            metadata=(("deliberation_prompt_digest", deliberation_digest()),),
        )

    async def explore(
        self,
        question: str,
        *,
        context: str = "",
        evidence: Sequence[EvidenceItem] = (),
        response: ResponseRequirements | None = None,
        settings: CouncilSettings | None = None,
    ) -> CouncilOutcome:
        """Frame the question and gather one independent perspective per seat. No answer.

        Raises ``CouncilCancelled`` (carrying the partial outcome) if cancelled.
        """
        request = self.build_request(
            question, context=context, evidence=evidence, response=response, settings=settings
        )
        run_id = self._ids.next_id("explore")
        runner = StagedCouncilRunner(
            registry=self._registry,
            stages=[FrameStage(self._prompts, self._screen), PerspectiveStage(self._prompts)],
            invoker=self._invoker_factory(self._profile, run_id),
            clock=SystemClock(),
            ids=self._ids,
            trace_sink=RequestTraceSink(),
        )
        if current_trace() is not None:
            return await runner.run(request, plan=EXPLORATION_PLAN)
        with trace_request(request.request_id):
            return await runner.run(request, plan=EXPLORATION_PLAN)
