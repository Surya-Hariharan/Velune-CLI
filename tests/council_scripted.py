"""Scripted doubles for the deliberative stages: committed prompt text, valid JSON replies, views.

``StaticPrompts`` serves the real committed wording straight from the prompt module, so tests
exercise the text that ships. ``valid_frame_json`` / ``valid_perspective_json`` build replies that
embed a per-seat sentinel so tests can look for contamination in the messages actually sent.
"""

from __future__ import annotations

import json

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

    def shared_prompt(self) -> str:
        return PROMPTS["council.general.shared"]

    def role_prompt(self, seat_id: str, stage: StageId) -> str:
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
