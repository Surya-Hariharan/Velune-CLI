"""Scripted doubles for the deliberative stages: committed prompt text, valid JSON replies, views.

``StaticPrompts`` serves the real committed wording straight from the prompt module, so tests
exercise the text that ships. ``valid_frame_json`` / ``valid_perspective_json`` build replies that
embed a per-seat sentinel so tests can look for contamination in the messages actually sent.
"""

from __future__ import annotations

import json
import re

from velune.cognition.prompts._deliberation import PROMPTS
from velune.council.contracts import Frame
from velune.council.domain import ArtifactKind, StageId
from velune.council.profiles import GENERAL_PROFILE
from velune.council.request import CouncilRequest
from velune.council.stages import VisibilityPolicy
from velune.council.state import ArtifactStore, StageView, StoredArtifact

PERSPECTIVE_SEATS = tuple(s.id for s in GENERAL_PROFILE.perspective_seats)


class StaticPrompts:
    """``PromptSource`` over the committed deliberation prompts (no premium layer, no I/O)."""

    def shared_prompt(self, stage: StageId) -> str:
        return PROMPTS.get(
            f"council.general.shared.{stage.value}", PROMPTS["council.general.shared"]
        )

    def role_prompt(self, seat_id: str, stage: StageId) -> str:
        if stage in (StageId.REVIEW, StageId.REVISION):
            return (
                PROMPTS[f"council.general.lens.{seat_id}"]
                + "\n\n"
                + PROMPTS[f"council.general.mode.{stage.value}"]
            )
        return PROMPTS[f"council.general.{seat_id}"]


def sentinel(seat_id: str, tag: str = "") -> str:
    return f"SENT-{seat_id.upper()}{('-' + tag) if tag else ''}-7f3a"


def valid_frame_json(**overrides: object) -> str:
    body: dict = {
        "question_restated": "How should a small team choose a database?",
        "problem_type": "decision",
        "constraints": ["budget is limited"],
        "ambiguities": [{"issue": "team size", "working_assumption": "about five engineers"}],
        "dimensions": ["cost", "operability", "scale"],
        "missing_information": ["expected data volume"],
        "needs_clarification": False,
    }
    body.update(overrides)
    return json.dumps(body)


def valid_perspective_json(seat_id: str, tag: str = "", /, **overrides: object) -> str:
    mark = sentinel(seat_id, tag)
    body: dict = {
        "position": f"{mark} position of the {seat_id}",
        "claims": [
            {
                "text": f"{mark} first claim",
                "status": "inferred",
                "support": "because of the stated constraints",
                "confidence": 0.6,
            },
            {
                "text": f"{mark} second claim",
                "status": "assumed",
                "confidence": 0.4,
                "depends_on": [1],
            },
            {"text": f"{mark} third claim", "status": "uncertain", "confidence": 0.3},
        ],
        "assumptions": [f"{mark} assumption"],
        "uncertainties": [f"{mark} uncertainty"],
        "confidence": 0.55,
        "rationale": f"{mark} rationale",
    }
    body.update(overrides)
    return json.dumps(body)


def claim_ids_of(seat_id: str, count: int = 3) -> frozenset[str]:
    """The ids ``valid_perspective_json`` yields for ``seat_id`` (core-assigned by position)."""
    prefix = GENERAL_PROFILE.seat(seat_id).claim_prefix
    return frozenset(f"{prefix}-{n}" for n in range(1, count + 1))


def valid_review_json(
    reviewer: str, targets: tuple[str, ...] | list[str], tag: str = "", /, **overrides: object
) -> str:
    """A valid R2 reply from ``reviewer`` covering ``targets``, citing each target's own claim ids."""
    reviews = []
    for target in targets:
        mark = sentinel(f"{reviewer}-on-{target}", tag)
        prefix = GENERAL_PROFILE.seat(target).claim_prefix
        reviews.append(
            {
                "target": target,
                "steelman": f"{mark} best point",
                "agreements": [f"{prefix}-2"],
                "disagreements": [
                    {
                        "claim_id": f"{prefix}-1",
                        "objection": f"{mark} objection",
                        "kind": "unsupported",
                        "severity": "major",
                        "suggested_resolution": f"{mark} resolution",
                    }
                ],
                "confidence": 0.6,
            }
        )
    body: dict = {"reviews": reviews}
    body.update(overrides)
    return json.dumps(body)


def make_request(**kw) -> CouncilRequest:
    base = {"request_id": "req-1", "question": "Which database should we pick?"}
    base.update(kw)
    return CouncilRequest(**base)


def make_view(
    stage: StageId,
    viewer: str,
    request: CouncilRequest | None = None,
    store: ArtifactStore | None = None,
) -> StageView:
    return StageView(
        policy=VisibilityPolicy(),
        stage=stage,
        viewer=viewer,
        request=request or make_request(),
        store=store or ArtifactStore(),
    )


def store_with_frame(frame: Frame) -> ArtifactStore:
    store = ArtifactStore()
    store.commit(
        (
            StoredArtifact(
                kind=ArtifactKind.FRAME,
                stage=StageId.FRAME,
                author="moderator",
                payload=frame,
            ),
        )
    )
    return store


TARGETS_TAG = re.compile(r"<targets>(.*?)</targets>")


def targets_in(call) -> list[str]:
    """The targets a recorded review call asked for (read from its system prompt)."""
    system = next(m.content for m in call.messages if m.role == "system")
    return TARGETS_TAG.search(system).group(1).split(", ")


def review_reply(tag: str = ""):
    """A script entry answering any review call with a valid reply for exactly its targets."""

    def reply(call) -> str:
        return valid_review_json(call.seat_id, targets_in(call), tag)

    return reply


REQUIRED_TAG = re.compile(r"<required_responses>(.*?)</required_responses>")


def required_in(call) -> list[dict]:
    """The objections a recorded revision call says must be answered."""
    system = next(m.content for m in call.messages if m.role == "system")
    return json.loads(REQUIRED_TAG.search(system).group(1))


def valid_revision_json(seat_id: str, required: list[dict], tag: str = "", /, **overrides) -> str:
    """A revising reply: accepts every required objection and edits the seat's first claim."""
    prefix = GENERAL_PROFILE.seat(seat_id).claim_prefix
    mark = sentinel(f"{seat_id}-revised", tag)
    body: dict = {
        "responses": [
            {**item, "decision": "accept", "note": f"{mark} accepted {item['reviewer']}"}
            for item in required
        ],
        "edits": [
            {
                "op": "modify",
                "claim_id": f"{prefix}-1",
                "text": f"{mark} revised first claim",
                "reason": f"{mark} reason",
                "caused_by": [required[0]],
            }
        ],
        "confidence": 0.5,
        "remaining_uncertainties": [f"{mark} still unknown"],
    }
    body.update(overrides)
    return json.dumps(body)


def revision_reply(tag: str = "", **overrides):
    """A script entry answering any revision call with a valid revising reply."""

    def reply(call) -> str:
        return valid_revision_json(call.seat_id, required_in(call), tag, **overrides)

    return reply


def reaffirming_reply(tag: str = ""):
    """A script entry that rejects every objection and changes nothing."""

    def reply(call) -> str:
        mark = sentinel(f"{call.seat_id}-kept", tag)
        responses = [
            {**item, "decision": "reject", "note": f"{mark} rejected"} for item in required_in(call)
        ]
        return json.dumps({"responses": responses})

    return reply
