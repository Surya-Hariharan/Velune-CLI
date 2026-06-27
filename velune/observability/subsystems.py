"""Runtime module wiring for observability subsystems.

Registers a :class:`~velune.observability.trace_sink.TraceSink` as a
lifecycle-managed subsystem so that, for the duration of any real Velune
session, every event emitted on the :class:`~velune.events.CognitiveBus` is
persisted to the workspace trace log and later replayable via ``velune trace``.

The subsystem is failure-isolated: attaching the sink can never abort runtime
startup, because observability must not be able to take down the system it
observes.
"""

from __future__ import annotations

import logging

from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule

logger = logging.getLogger("velune.observability.module")


class _TraceSinkSubsystem:
    """Lifecycle adapter that attaches/detaches the trace sink to the bus."""

    def __init__(self, env: RuntimeEnvironment) -> None:
        from velune.observability.trace_sink import TraceSink, trace_log_for_workspace

        self._container = env.container
        self._sink = TraceSink(trace_log_for_workspace(env.workspace))

    async def initialize(self) -> None:
        try:
            if not self._container.has("runtime.bus"):
                return
            bus = self._container.get("runtime.bus")
            await self._sink.attach(bus)
        except Exception as exc:  # never abort startup over tracing
            logger.debug("Trace sink failed to attach: %s", exc)

    async def shutdown(self) -> None:
        try:
            await self._sink.detach()
        except Exception as exc:
            logger.debug("Trace sink failed to detach: %s", exc)


def _create_trace_sink(env: RuntimeEnvironment) -> _TraceSinkSubsystem:
    return _TraceSinkSubsystem(env)


OBSERVABILITY_MODULES = [
    SubsystemModule(
        name="trace_sink",
        factory=_create_trace_sink,
        container_key="runtime.trace_sink",
        lifecycle_key="trace_sink",
        dependencies=["runtime.bus"],
        tier=0,
    )
]
