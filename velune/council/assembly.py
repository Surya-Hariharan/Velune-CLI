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
from dataclasses import dataclass

from pydantic import BaseModel

from velune.cognition.execution_trace import CallReason
from velune.council.contracts import Critique, Frame, Perspective
from velune.council.domain import ArtifactKind, StageId
from velune.council.drafts import FrameDraft, PerspectiveDraft, fallback_frame
from velune.council.ports import PromptSource, SeatCall, SeatMessage
from velune.council.profiles import RoleProfile, SeatSpec
from velune.council.reviewdrafts import ReviewReplyDraft
from velune.council.revisiondrafts import RevisionDraft, required_responses
from velune.council.serialization import canonical_json
from velune.council.state import ClaimDigest, StageView


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


def render_frame_context(view: StageView, frame: Frame) -> str:
    """The text an R1 seat would receive for this frame, minus the evidence R0 may not read.

    Used to screen a Moderator-authored frame before it is committed: the request blocks followed
    by the same ``<frame>`` block ``build_perspective_call`` appends.
    """
    return "\n".join([*_request_blocks(view), _frame_block(frame)])


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
        prompts.shared_prompt(StageId.FRAME),
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
        prompts.shared_prompt(StageId.PERSPECTIVES),
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


# ── R2: a reviewer's request ────────────────────────────────────────────────

_PERSPECTIVE_BOOKKEEPING = ("seat_id", "schema_version")
_DIGEST_FIELDS = ("id", "text", "status", "support")


@dataclass(frozen=True)
class ReviewMaterial:
    """Everything a reviewer may be shown, as the stage gathered it from the reviewer's own view.

    ``order`` is the profile's perspective-seat order, which fixes the order peers are shown in. A
    peer that did not deliver is simply not here.
    """

    own: Perspective
    order: tuple[str, ...]
    full: tuple[tuple[SeatSpec, Perspective], ...] = ()
    digests: tuple[tuple[SeatSpec, ClaimDigest], ...] = ()

    @property
    def targets(self) -> tuple[SeatSpec, ...]:
        specs = (*(spec for spec, _ in self.full), *(spec for spec, _ in self.digests))
        return tuple(sorted(specs, key=lambda spec: self.order.index(spec.id)))

    def shown_claim_ids(self) -> dict[str, frozenset[str]]:
        """Target seat -> the claim ids this reviewer was shown for it."""
        shown = {spec.id: frozenset(c.id for c in p.claims) for spec, p in self.full}
        shown.update({spec.id: frozenset(c.id for c in d.claims) for spec, d in self.digests})
        return shown


def _perspective_fields(perspective: Perspective) -> dict:
    fields = json.loads(canonical_json(perspective))
    for bookkeeping in _PERSPECTIVE_BOOKKEEPING:
        fields.pop(bookkeeping, None)
    return fields


def _compact(fields: object) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def render_own_perspective(perspective: Perspective) -> str:
    return f"<own_perspective>{neutralize(_compact(_perspective_fields(perspective)))}</own_perspective>"


def render_peer_perspective(spec: SeatSpec, perspective: Perspective) -> str:
    """A peer's full perspective, as a reviewer or as the screen sees it."""
    body = neutralize(_compact(_perspective_fields(perspective)))
    return f'<peer seat="{spec.id}" role="{neutralize(spec.display_name)}">{body}</peer>'


def render_peer_claims(spec: SeatSpec, digest: ClaimDigest) -> str:
    """A peer's claims only: id, text, label and support. No position, rationale or confidence."""
    rows = []
    for claim in digest.claims:
        fields = json.loads(canonical_json(claim))
        rows.append({name: fields[name] for name in _DIGEST_FIELDS})
    body = neutralize(_compact(rows))
    return (
        f'<peer_claims seat="{spec.id}" role="{neutralize(spec.display_name)}">{body}</peer_claims>'
    )


def build_review_call(
    *,
    view: StageView,
    seat: SeatSpec,
    material: ReviewMaterial,
    prompts: PromptSource,
    timeout_s: float,
) -> SeatCall:
    """The R2 request for one reviewer: the shared request, its own view, and only its assigned peers.

    Everything comes from the reviewer's ``StageView`` and the material the stage gathered through
    it. Other reviewers' critiques, unassigned peers and later stages are not inputs.
    """
    targets = material.targets
    system = _system(
        prompts.shared_prompt(StageId.REVIEW),
        prompts.role_prompt(seat.id, StageId.REVIEW),
        [
            _seat_block(seat),
            f'<stage id="{StageId.REVIEW.value}"/>',
            f"<targets>{', '.join(spec.id for spec in targets)}</targets>",
        ],
        ReviewReplyDraft,
    )
    blocks = _request_blocks(view)
    for item in view.evidence():
        source = f' source="{neutralize(item.source)}"' if item.source else ""
        blocks.append(
            f'<evidence id="{neutralize(item.id)}"{source}>{neutralize(item.text)}</evidence>'
        )
    blocks.append(_frame_block(frame_for(view)))
    blocks.append(render_own_perspective(material.own))
    full = {spec.id: (spec, perspective) for spec, perspective in material.full}
    digests = {spec.id: (spec, digest) for spec, digest in material.digests}
    for spec in targets:
        if spec.id in full:
            blocks.append(render_peer_perspective(*full[spec.id]))
        else:
            blocks.append(render_peer_claims(*digests[spec.id]))
    return SeatCall(
        seat_id=seat.id,
        kind=seat.kind,
        stage=StageId.REVIEW,
        messages=(
            SeatMessage(role="system", content=system),
            SeatMessage(role="user", content="\n".join(blocks)),
        ),
        reason=CallReason.REVIEW,
        timeout_s=timeout_s,
    )


def render_perspective_context(view: StageView, spec: SeatSpec, perspective: Perspective) -> str:
    """What R2 would send onward for a delivered perspective, for screening before it is committed."""
    return "\n".join(
        [
            *_request_blocks(view),
            _frame_block(frame_for(view)),
            render_peer_perspective(spec, perspective),
        ]
    )


_CRITIQUE_BOOKKEEPING = ("reviewer_seat", "target_seat", "schema_version")
_CRITIQUE_FIELDS = ("steelman", "agreements", "disagreements", "no_material_issues", "confidence")


def render_critique(reviewer: SeatSpec, critique: Critique) -> str:
    """A critique as its target sees it: objections numbered from 1 so a revision can cite them."""
    fields = json.loads(canonical_json(critique))
    body = {name: fields[name] for name in _CRITIQUE_FIELDS}
    body["disagreements"] = [
        {"n": index, **item} for index, item in enumerate(body["disagreements"], start=1)
    ]
    return (
        f'<critique from="{reviewer.id}" role="{neutralize(reviewer.display_name)}">'
        f"{neutralize(_compact(body))}</critique>"
    )


def render_critique_context(view: StageView, critiques: Sequence[tuple[SeatSpec, Critique]]) -> str:
    """What R3 would send onward for these critiques, for screening before they are committed."""
    return "\n".join(
        [*_request_blocks(view), *(render_critique(spec, item) for spec, item in critiques)]
    )


# ── R3: a seat's revision request ───────────────────────────────────────────


@dataclass(frozen=True)
class RevisionMaterial:
    """A seat's own R1 perspective and the critiques addressed to it, both read through its own view.

    ``critiques`` are in profile order of the reviewer, which fixes how objections are numbered.
    """

    own: Perspective
    critiques: tuple[tuple[SeatSpec, Critique], ...]

    @property
    def reviewed(self) -> tuple[tuple[str, Critique], ...]:
        return tuple((spec.id, critique) for spec, critique in self.critiques)


def build_revision_call(
    *,
    view: StageView,
    seat: SeatSpec,
    material: RevisionMaterial,
    prompts: PromptSource,
    timeout_s: float,
) -> SeatCall:
    """The R3 request for one seat: the shared request, its own R1 perspective, its own critiques.

    Critiques addressed to anyone else, other seats' perspectives and revisions, and later stages are
    not inputs; the stage hands over only what the seat's own view granted.
    """
    required = [
        {"reviewer": reviewer, "objection": number}
        for reviewer, number in required_responses(material.reviewed)
    ]
    system = _system(
        prompts.shared_prompt(StageId.REVISION),
        prompts.role_prompt(seat.id, StageId.REVISION),
        [
            _seat_block(seat),
            f'<stage id="{StageId.REVISION.value}"/>',
            f"<required_responses>{_compact(required)}</required_responses>",
        ],
        RevisionDraft,
    )
    blocks = _request_blocks(view)
    for item in view.evidence():
        source = f' source="{neutralize(item.source)}"' if item.source else ""
        blocks.append(
            f'<evidence id="{neutralize(item.id)}"{source}>{neutralize(item.text)}</evidence>'
        )
    blocks.append(_frame_block(frame_for(view)))
    blocks.append(render_own_perspective(material.own))
    blocks.extend(render_critique(spec, critique) for spec, critique in material.critiques)
    return SeatCall(
        seat_id=seat.id,
        kind=seat.kind,
        stage=StageId.REVISION,
        messages=(
            SeatMessage(role="system", content=system),
            SeatMessage(role="user", content="\n".join(blocks)),
        ),
        reason=CallReason.REVISION,
        timeout_s=timeout_s,
    )
