"""Pure assembly of the messages a seat is sent.

Every message is built from a ``StageView`` (the only read path a stage has), the seat's own spec and
static prompt text from the ``PromptSource`` port. Nothing else can reach a message, which is what
makes the independence guarantee structural: a peer's output is not an input to these functions.

Untrusted text (the question, the context, evidence and the frame, which is itself model output) is
escaped so it cannot close a tag or pose as a different block, and the prompts tell the model such
blocks are data.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import BaseModel

from velune.council.contracts import Frame
from velune.council.domain import ArtifactKind, StageId
from velune.council.drafts import FrameDraft, PerspectiveDraft, fallback_frame
from velune.council.ports import PromptSource, SeatCall, SeatMessage
from velune.council.profiles import RoleProfile, SeatSpec
from velune.council.serialization import canonical_json
from velune.council.state import StageView


def neutralize(text: str) -> str:
    """Escape ``& < >`` so untrusted text cannot open or close a block."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_schema(model: type[BaseModel]) -> str:
    """The model's JSON schema, generated from the draft itself so prompt and parser cannot drift."""
    return json.dumps(model.model_json_schema(), sort_keys=True, separators=(",", ":"))


def _seat_block(spec: SeatSpec) -> str:
    return (
        f'<seat id="{spec.id}">{neutralize(spec.display_name)}: {neutralize(spec.objective)}</seat>'
    )


def _system(shared: str, role: str, extra: Sequence[str], schema: type[BaseModel]) -> str:
    parts = [shared.strip(), role.strip(), *extra, f"<schema>{render_schema(schema)}</schema>"]
    return "\n\n".join(parts)


def _request_blocks(view: StageView) -> list[str]:
    blocks = [f"<question>{neutralize(view.question())}</question>"]
    context = view.context().strip()
    if context:
        blocks.append(f"<context>{neutralize(context)}</context>")
    wanted = view.requirements()
    lines = [
        f"{name}: {value}"
        for name, value in (
            ("language", wanted.language),
            ("format", wanted.format),
            ("length", wanted.length),
            ("audience", wanted.audience),
        )
        if value
    ]
    if lines:
        blocks.append(f"<requirements>{neutralize(chr(10).join(lines))}</requirements>")
    return blocks


def frame_for(view: StageView) -> Frame:
    """The frame this stage may read, or the deterministic one when R0 left none."""
    found = view.read(ArtifactKind.FRAME)
    if found and isinstance(found[0].payload, Frame):
        return found[0].payload
    return fallback_frame(view.question())


def _frame_block(frame: Frame) -> str:
    # Typed fields only: no raw Moderator text, and no degraded/language/version bookkeeping.
    fields = json.loads(canonical_json(frame))
    for bookkeeping in ("degraded", "language", "schema_version"):
        fields.pop(bookkeeping, None)
    return f"<frame>{neutralize(json.dumps(fields, sort_keys=True, separators=(',', ':')))}</frame>"


def build_frame_call(
    *,
    view: StageView,
    profile: RoleProfile,
    prompts: PromptSource,
    timeout_s: float,
) -> SeatCall:
    """The R0 request: question, context and requirements only. No evidence, no other seats."""
    spec = profile.moderator
    vocabulary = ", ".join(profile.problem_types)
    system = _system(
        prompts.shared_prompt(),
        prompts.role_prompt(spec.id, StageId.FRAME),
        [_seat_block(spec), f"<problem_types>{neutralize(vocabulary)}</problem_types>"],
        FrameDraft,
    )
    return SeatCall(
        seat_id=spec.id,
        kind=spec.kind,
        stage=StageId.FRAME,
        messages=(
            SeatMessage(role="system", content=system),
            SeatMessage(role="user", content="\n".join(_request_blocks(view))),
        ),
        timeout_s=timeout_s,
    )


def build_perspective_call(
    *,
    view: StageView,
    seat: SeatSpec,
    prompts: PromptSource,
    timeout_s: float,
) -> SeatCall:
    """The R1 request for one seat: the request, identical evidence, the typed frame, its own spec."""
    system = _system(
        prompts.shared_prompt(),
        prompts.role_prompt(seat.id, StageId.PERSPECTIVES),
        [_seat_block(seat)],
        PerspectiveDraft,
    )
    blocks = _request_blocks(view)
    for item in view.evidence():
        source = f' source="{neutralize(item.source)}"' if item.source else ""
        blocks.append(
            f'<evidence id="{neutralize(item.id)}"{source}>{neutralize(item.text)}</evidence>'
        )
    blocks.append(_frame_block(frame_for(view)))
    return SeatCall(
        seat_id=seat.id,
        kind=seat.kind,
        stage=StageId.PERSPECTIVES,
        messages=(
            SeatMessage(role="system", content=system),
            SeatMessage(role="user", content="\n".join(blocks)),
        ),
        timeout_s=timeout_s,
    )
