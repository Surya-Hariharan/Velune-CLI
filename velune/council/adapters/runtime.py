"""Binds the core's ports to Velune's existing runtime: model routing, providers, fallback, trace.

``RuntimeSeatInvoker`` implements ``SeatInvoker`` by *calling* the Phase 0 machinery rather than
re-implementing it: ``BaseCouncilAgent.deliberate(strict=True)`` does the timeout, retry, firewall
scan, fallback chain and key invalidation, and ``CouncilAgentFactory`` supplies the model and the
fallbacks for a seat's routing role. This adapter only translates between the two vocabularies.

It is the one module under ``velune.council`` allowed to import providers, models and the legacy
council, and it does so lazily so that importing the core never loads them.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from velune.cognition import execution_trace as et
from velune.cognition.execution_trace import NodeType, current_trace, trace_seat
from velune.council.adapters.legacy import seat_status_from_agent_kind
from velune.council.domain import StageId
from velune.council.ports import SeatCall
from velune.council.profiles import RoleProfile, SeatSpec
from velune.council.results import ModelRef, SeatResult, SeatStatus
from velune.council.trace import CouncilTraceEvent, TraceEventKind

if TYPE_CHECKING:
    from velune.cognition.council.factory import CouncilAgentFactory

# Council stage -> execution-graph node type (R2/R4/R5 reuse the existing members).
STAGE_NODE_TYPES: dict[StageId, NodeType] = {
    StageId.FRAME: NodeType.FRAMING,
    StageId.PERSPECTIVES: NodeType.PERSPECTIVES,
    StageId.REVIEW: NodeType.REVIEW,
    StageId.REVISION: NodeType.REVISION,
    StageId.ARBITRATION: NodeType.ARBITRATION,
    StageId.SYNTHESIS: NodeType.SYNTHESIS,
}

_STAGE_END_EVENTS = frozenset(
    {
        TraceEventKind.STAGE_FINISHED,
        TraceEventKind.STAGE_ERROR,
        TraceEventKind.STAGE_TIMEOUT,
        TraceEventKind.CONTRACT_VIOLATION,
        TraceEventKind.CANCELLED,
    }
)


class SystemClock:
    """Real monotonic time, for production wiring (the core never reads a clock itself)."""

    def monotonic(self) -> float:
        return time.monotonic()


class UuidIds:
    """Random run identifiers, for production wiring."""

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _model_ref(identifier: str) -> ModelRef:
    provider_id, _, model_id = identifier.partition("/")
    return ModelRef(provider_id=provider_id, model_id=model_id)


class RuntimeSeatInvoker:
    """``SeatInvoker`` over the existing provider / fallback / trace machinery."""

    def __init__(self, *, profile: RoleProfile, factory: CouncilAgentFactory, run_id: str) -> None:
        self._seats: dict[str, SeatSpec] = {spec.id: spec for spec in profile.all_seats}
        self._factory = factory
        self._run_id = run_id

    @classmethod
    def from_container(
        cls, container: Any, profile: RoleProfile, run_id: str
    ) -> RuntimeSeatInvoker:
        """Build from the runtime container, the same lookup ``velune ask`` uses."""
        orchestrator = container.get("runtime.council_orchestrator")
        return cls(profile=profile, factory=orchestrator.agent_factory, run_id=run_id)

    async def invoke(self, call: SeatCall) -> SeatResult[str]:
        """Run one seat call. Never raises, except ``CancelledError``."""

        def failed(status: SeatStatus, message: str = "", **kw: Any) -> SeatResult[str]:
            return SeatResult.failure(
                seat_id=call.seat_id,
                kind=call.kind,
                stage=call.stage,
                status=status,
                message=message,
                **kw,
            )

        spec = self._seats.get(call.seat_id)
        if spec is None or spec.kind is not call.kind:
            return failed(SeatStatus.SKIPPED, "seat is not part of this profile")

        from velune.cognition.council.base import (
            BaseCouncilAgent,
            CouncilAgentError,
            is_failure_text,
        )
        from velune.models.specializations import CouncilRole

        role = CouncilRole(spec.routing_role.value)
        try:
            model = self._factory.get_role_mapping(self._run_id)[role]
            provider = self._factory.provider_registry.get_or_raise(model.provider_id)
            fallbacks = self._factory.fallbacks_for(role, model)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return failed(
                SeatStatus.PROVIDER_ERROR, f"no model for {role.value}: {type(exc).__name__}"
            )

        class _SeatAgent(BaseCouncilAgent):
            """A seat is an agent named for the seat id: no panel, no streaming UX."""

        system = "\n\n".join(m.content for m in call.messages if m.role == "system")
        history = [
            {"role": m.role, "content": m.content} for m in call.messages if m.role != "system"
        ]
        agent = _SeatAgent(
            role=role,
            model=model,
            provider=provider,
            system_prompt=system,
            live_lock=None,
            fallback_providers=fallbacks,
            seat_name=spec.id,
        )
        fell_back: list[str] = []
        agent.on_fallback = lambda _seat, _from, to: fell_back.append(to)
        primary = ModelRef(provider_id=model.provider_id, model_id=model.model_id)
        started = time.monotonic()

        def identity() -> dict[str, Any]:
            return {
                "model": _model_ref(fell_back[0]) if fell_back else primary,
                "attempts": 1 + len(fell_back),
                "fallback_used": bool(fell_back),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }

        try:
            with trace_seat(call.seat_id, call.reason):
                text = await asyncio.wait_for(
                    agent.deliberate(
                        history,
                        temperature=call.temperature,
                        max_tokens=call.max_tokens,
                        strict=True,
                    ),
                    timeout=call.timeout_s,
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return failed(
                SeatStatus.TIMEOUT, f"timed out after {call.timeout_s:.0f}s", **identity()
            )
        except CouncilAgentError as exc:
            status = seat_status_from_agent_kind(exc.kind)
            return failed(status, f"{exc.kind}: {exc.detail}", **identity())
        except ValueError as exc:
            blocked = str(exc).startswith("Security:")
            return failed(
                SeatStatus.BLOCKED if blocked else SeatStatus.PROVIDER_ERROR,
                str(exc)[:200],
                **identity(),
            )
        except Exception as exc:
            return failed(SeatStatus.PROVIDER_ERROR, type(exc).__name__, **identity())
        if is_failure_text(text):
            return failed(SeatStatus.EMPTY, "the model returned no usable text", **identity())
        return SeatResult(
            seat_id=call.seat_id,
            kind=call.kind,
            stage=call.stage,
            status=SeatStatus.OK,
            payload=text,
            **identity(),
        )


class RequestTraceSink:
    """Projects core trace events onto the active ``RequestTrace`` (one node per stage).

    The node is made current for the duration of the stage so provider calls made beneath it are
    attributed to it, exactly like the legacy council's ``trace_node`` scopes. With no active
    request trace it does nothing.
    """

    def __init__(self) -> None:
        self._open: dict[StageId, Any] = {}
        self._tokens: dict[StageId, Any] = {}

    def emit(self, event: CouncilTraceEvent) -> None:
        trace = current_trace()
        if trace is None or event.stage is None:
            return
        stage = event.stage
        if event.event is TraceEventKind.STAGE_STARTED:
            node = trace.open_node(STAGE_NODE_TYPES[stage], f"council {stage.value}")
            self._open[stage] = node
            self._tokens[stage] = et._active_node.set(node.node_id)
        elif event.event in _STAGE_END_EVENTS:
            token = self._tokens.pop(stage, None)
            if token is not None:
                try:
                    et._active_node.reset(token)
                except ValueError:  # reset from another context (cancellation); node closes below
                    pass
            node = self._open.pop(stage, None)
            if node is not None:
                ok = event.event is TraceEventKind.STAGE_FINISHED and event.status != "failed"
                node.finish("ok" if ok else "error", event.status or event.detail)
