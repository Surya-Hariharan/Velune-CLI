"""Helpers for running R0/R1 end to end: a scripted provider and ready-made runners.

``ScriptedProvider`` answers each seat with canned JSON (or a failure) and records every request, so
tests can inspect the messages that would actually be sent to a model. Seats are identified by the
``<seat id="...">`` tag the assembly puts in every system prompt.
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
    valid_frame_json,
    valid_perspective_json,
)
from velune.cognition.council.factory import CouncilAgentFactory
from velune.council.adapters.runtime import RuntimeSeatInvoker
from velune.council.frame import FrameStage
from velune.council.perspectives import PerspectiveStage
from velune.council.profiles import GENERAL_PROFILE, default_registry
from velune.council.runner import StagedCouncilRunner
from velune.council.stages import EXPLORATION_PLAN

SEAT_TAG = re.compile(r'<seat id="([a-z_]+)">')


def seat_in(request: Any) -> str:
    """The seat a recorded provider request was built for (``unknown`` if none)."""
    system = next((m["content"] for m in request.messages if m["role"] == "system"), "")
    found = SEAT_TAG.search(system)
    return found.group(1) if found else "unknown"


class ScriptedProvider(FakeProvider):
    """Per-seat scripted replies. An entry is text, an exception, a ``Delay`` or a callable.

    Entries are consumed in order and the last one repeats. Unscripted seats answer with a valid
    reply carrying a per-seat sentinel; ``tag`` varies those sentinels between runs so tests can
    prove a seat's messages do not depend on what its peers said.
    """

    def __init__(
        self,
        scripts: dict[str, list[Any]] | None = None,
        *,
        provider_id: str = "fake",
        tag: str = "",
    ) -> None:
        super().__init__(provider_id, responder=self._answer)
        self.scripts = {seat: list(entries) for seat, entries in (scripts or {}).items()}
        self.tag = tag
        self.log: list[tuple[str, Any]] = []

    def _answer(self, _seat: str, request: Any) -> Any:
        seat = seat_in(request)
        self.log.append((seat, request))
        queue = self.scripts.get(seat)
        if queue:
            entry = queue[0]
            if len(queue) > 1:
                queue.pop(0)
            return entry(request) if callable(entry) else entry
        if seat == "moderator":
            return valid_frame_json(question_restated=f"FRAME-{self.tag or 'base'} restated")
        return valid_perspective_json(seat, self.tag)

    def requests_for(self, seat: str) -> list[Any]:
        return [request for who, request in self.log if who == seat]

    def messages_for(self, seat: str) -> list[list[dict[str, str]]]:
        return [request.messages for request in self.requests_for(seat)]


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
    clock: Callable[[], Any] = FakeClock,
    **kw: Any,
) -> StagedCouncilRunner:
    source = prompts or StaticPrompts()
    return StagedCouncilRunner(
        registry=default_registry(),
        stages=stages or [FrameStage(source), PerspectiveStage(source)],
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
