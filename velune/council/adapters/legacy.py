"""A pure mapper from today's coding-council result onto the neutral ``CouncilOutcome``.

This proves the new types can carry what the live pipeline already returns, without changing it:
nothing here runs a council, calls a provider or is called by any live path. It reads a plain dict,
so it imports nothing but the core.

Mapping (Phase 0 semantics preserved):

* ``is_timeout`` (wall clock hit, or every model call failed) -> ``failed``, no answer;
  the explanation moves to ``failure_summary`` and never to ``answer``.
* ``degraded`` with ``degradation_reasons`` -> ``degraded`` with those reasons as degradations.
* otherwise -> ``completed``; the Coder's proposal becomes an inert ``proposal`` artifact.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from velune.council.contracts import Artifact
from velune.council.domain import CouncilDomain
from velune.council.results import CouncilOutcome, OutcomeStatus, SeatStatus

CODING_PROFILE_ID = "coding"
_MAX_NOTE = 300

# CouncilAgentError.kind -> SeatStatus
AGENT_ERROR_KINDS: Mapping[str, SeatStatus] = {
    "timeout": SeatStatus.TIMEOUT,
    "provider": SeatStatus.PROVIDER_ERROR,
    "auth": SeatStatus.AUTH_ERROR,
    "empty": SeatStatus.EMPTY,
}

# typed-message ``status`` (a seat that abstained) -> SeatStatus
MESSAGE_STATUSES: Mapping[str, SeatStatus] = {
    "ok": SeatStatus.OK,
    "unavailable": SeatStatus.PROVIDER_ERROR,
    "unparseable": SeatStatus.UNPARSEABLE,
}

# ExecutionStatus.value -> OutcomeStatus (``completed`` is refined by the degraded flag)
EXECUTION_STATUSES: Mapping[str, OutcomeStatus] = {
    "completed": OutcomeStatus.COMPLETED,
    "failed": OutcomeStatus.FAILED,
    "interrupted": OutcomeStatus.CANCELLED,
}


def seat_status_from_agent_kind(kind: str) -> SeatStatus:
    return AGENT_ERROR_KINDS.get(kind, SeatStatus.PROVIDER_ERROR)


def seat_status_from_message_status(status: str) -> SeatStatus:
    return MESSAGE_STATUSES.get(status, SeatStatus.UNPARSEABLE)


def outcome_status_from_execution(status: str, *, degraded: bool = False) -> OutcomeStatus:
    mapped = EXECUTION_STATUSES.get(status, OutcomeStatus.FAILED)
    if mapped is OutcomeStatus.COMPLETED and degraded:
        return OutcomeStatus.DEGRADED
    return mapped


def _clip(text: Any) -> str:
    return str(text)[:_MAX_NOTE]


def outcome_from_legacy_result(
    result: Mapping[str, Any],
    *,
    request_id: str = "legacy",
    profile_id: str = CODING_PROFILE_ID,
) -> CouncilOutcome:
    """Map one ``CouncilOrchestrator.execute_task`` result onto a ``CouncilOutcome``."""
    summary = result.get("final_summary")
    text = summary if isinstance(summary, str) and summary.strip() else None
    base: dict[str, Any] = {
        "request_id": request_id,
        "profile_id": profile_id,
        "domain": CouncilDomain.CODING,
    }

    if result.get("is_timeout"):
        return CouncilOutcome(
            **base,
            status=OutcomeStatus.FAILED,
            failure_summary=_clip(text or "the council produced no answer"),
        )

    reasons = tuple(_clip(r) for r in (result.get("degradation_reasons") or ()))
    degraded = bool(result.get("degraded")) or bool(reasons)
    proposal = result.get("coder_proposal")
    artifacts = (
        (Artifact(kind="proposal", media_type="text/plain", content=proposal),)
        if isinstance(proposal, str) and proposal.strip()
        else ()
    )
    return CouncilOutcome(
        **base,
        status=OutcomeStatus.DEGRADED if degraded else OutcomeStatus.COMPLETED,
        answer=text,
        artifacts=artifacts,
        degradations=reasons,
    )
