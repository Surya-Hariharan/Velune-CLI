"""Helpers for running R0-R2 end to end: a scripted provider and ready-made runners.

``ScriptedProvider`` answers each seat with canned JSON (or a failure) and records every request, so
tests can inspect the messages that would actually be sent to a model. Seats are identified by the
``<seat id="...">`` tag the assembly puts in every system prompt, and from R2 on the stage by its
``<stage id="..."/>`` tag, because one seat now speaks in several stages.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from tests.council_core_fakes import CounterIds, FakeClock
from tests.council_fakes import FakeMapper, FakeProvider, FakeProviderRegistry
from tests.council_scripted import (
    StaticPrompts,
    make_request,
    review_reply,
    revision_reply,
    valid_frame_json,
    valid_perspective_json,
)
from velune.cognition.council.factory import CouncilAgentFactory
from velune.council.adapters.runtime import RuntimeSeatInvoker
from velune.council.domain import StageId
from velune.council.frame import FrameStage
from velune.council.perspectives import PerspectiveStage
from velune.council.profiles import GENERAL_PROFILE, default_registry
from velune.council.review import ReviewStage
from velune.council.revision import RevisionStage
from velune.council.runner import StagedCouncilRunner
from velune.council.stages import EXPLORATION_PLAN
from velune.council.topology import ProfileAssignments

SEAT_TAG = re.compile(r'<seat id="([a-z_]+)">')
STAGE_TAG = re.compile(r'<stage id="([a-z_]+)"/>')
UP_TO_REVIEW = (StageId.FRAME, StageId.PERSPECTIVES, StageId.REVIEW)
UP_TO_REVISION = (*UP_TO_REVIEW, StageId.REVISION)


def _system_of(request: Any) -> str:
    return next((m["content"] for m in request.messages if m["role"] == "system"), "")


def seat_in(request: Any) -> str:
    """The seat a recorded provider request was built for (``unknown`` if none)."""
    found = SEAT_TAG.search(_system_of(request))
    return found.group(1) if found else "unknown"


def stage_in(request: Any) -> str:
    """The stage a recorded request belongs to: ``frame``, ``perspectives``, ``review``, ..."""
    found = STAGE_TAG.search(_system_of(request))
    if found:
        return found.group(1)
    return "frame" if seat_in(request) == "moderator" else "perspectives"


class ScriptedProvider(FakeProvider):
    """Per-seat scripted replies. An entry is text, an exception, a ``Delay`` or a callable.

    Entries are consumed in order and the last one repeats. A script key is a seat id (R0/R1) or
    ``"<seat>:<stage>"`` for later stages. Unscripted seats answer with a valid reply carrying a
    per-seat sentinel; ``tag`` varies those sentinels between runs so tests can prove a seat's
    messages do not depend on what its peers said.
    """

    def __init__(
        self,
        scripts: dict[str, list[Any]] | None = None,
        *,
        provider_id: str = "fake",
        tag: str = "",
        review_tag: str | None = None,
    ) -> None:
        super().__init__(provider_id, responder=self._answer)
        self.scripts = {seat: list(entries) for seat, entries in (scripts or {}).items()}
        self.tag = tag
        self.review_tag = tag if review_tag is None else review_tag
        self.log: list[tuple[str, Any]] = []

    def _answer(self, _seat: str, request: Any) -> Any:
        seat, stage = seat_in(request), stage_in(request)
        self.log.append((seat, request))
        key = seat if stage in ("frame", "perspectives") else f"{seat}:{stage}"
        queue = self.scripts.get(key)
        if queue:
            entry = queue[0]
            if len(queue) > 1:
                queue.pop(0)
            return entry(request) if callable(entry) else entry
        if stage == "frame":
            return valid_frame_json(question_restated=f"FRAME-{self.tag or 'base'} restated")
        if stage == "review":
            return review_reply(self.review_tag)(_ShimCall(seat, request))
        if stage == "revision":
            return revision_reply(self.tag)(_ShimCall(seat, request))
        return valid_perspective_json(seat, self.tag)

    def requests_for(self, seat: str, stage: str | None = None) -> list[Any]:
        return [
            request
            for who, request in self.log
            if who == seat and (stage is None or stage_in(request) == stage)
        ]

    def messages_for(self, seat: str) -> list[list[dict[str, str]]]:
        return [request.messages for request in self.requests_for(seat)]


class _ShimCall:
    """Lets ``review_reply`` (written for ``SeatCall``) read a recorded provider request."""

    def __init__(self, seat: str, request: Any) -> None:
        self.seat_id = seat
        self.messages = [
            type("M", (), {"role": m["role"], "content": m["content"]})() for m in request.messages
        ]


def wire_review_reply(tag: str = ""):
    """A ``ScriptedProvider`` script entry answering a review request with a valid reply."""

    def reply(request: Any) -> str:
        return review_reply(tag)(_ShimCall(seat_in(request), request))

    return reply


def wire_revision_reply(tag: str = "", **overrides: Any):
    """A ``ScriptedProvider`` script entry answering a revision request with a valid reply."""

    def reply(request: Any) -> str:
        return revision_reply(tag, **overrides)(_ShimCall(seat_in(request), request))

    return reply


def runtime_invoker(provider: FakeProvider, mapper: Any = None, **factory: Any):
    extra = factory.pop("extra", ())
    registry = FakeProviderRegistry(provider, *extra)
    agents = CouncilAgentFactory(registry, mapper or FakeMapper(), **factory)
    return RuntimeSeatInvoker(profile=GENERAL_PROFILE, factory=agents, run_id="run-1")


def exploration_runner(
    invoker: Any,
    *,
    prompts: Any = None,
    stages: Any = None,
    sink: Any = None,
    screen: Any = None,
    clock: Callable[[], Any] = FakeClock,
    **kw: Any,
) -> StagedCouncilRunner:
    source = prompts or StaticPrompts()
    return StagedCouncilRunner(
        registry=default_registry(),
        stages=stages or [FrameStage(source, screen), PerspectiveStage(source)],
        invoker=invoker,
        clock=clock(),
        ids=CounterIds(),
        trace_sink=sink,
        **kw,
    )


async def explore(invoker: Any, request: Any = None, *, sink: Any = None, **kw: Any):
    """Run the exploration plan and return its outcome."""
    runner = exploration_runner(invoker, sink=sink, **kw)
    return await runner.run(request or make_request(), plan=EXPLORATION_PLAN)


def deliberation_runner(
    invoker: Any,
    *,
    prompts: Any = None,
    stages: Any = None,
    sink: Any = None,
    screen: Any = None,
    clock: Callable[[], Any] = FakeClock,
    **kw: Any,
) -> StagedCouncilRunner:
    """A runner over the stages that exist, with the review graph installed as the assignments."""
    source = prompts or StaticPrompts()
    kw.setdefault("assignments", ProfileAssignments(GENERAL_PROFILE))
    return StagedCouncilRunner(
        registry=default_registry(),
        stages=stages
        or [
            FrameStage(source, screen),
            PerspectiveStage(source, screen),
            ReviewStage(source, screen),
            RevisionStage(source),
        ],
        invoker=invoker,
        clock=clock(),
        ids=CounterIds(),
        trace_sink=sink,
        **kw,
    )


async def deliberate(
    invoker: Any,
    request: Any = None,
    *,
    sink: Any = None,
    plan: Any = UP_TO_REVIEW,
    **kw: Any,
):
    """Run frame, perspectives and review and return the outcome."""
    runner = deliberation_runner(invoker, sink=sink, **kw)
    return await runner.run(request or make_request(), plan=plan)
