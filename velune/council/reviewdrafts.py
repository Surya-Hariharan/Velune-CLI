"""What a reviewer is asked to return in R2, and the pure conversion into ``Critique`` contracts.

The wire shape is smaller than ``Critique``: one reply holds a review for each of the reviewer's
assigned targets, and every kind of objection (a doubtful assumption, an unsupported claim, a
counterexample, something missing) is one ``Disagreement`` with a ``kind``. The core, not the model,
owns identity: the reviewer is the seat that was called, each critique's target must be one of the
targets the reviewer was shown, and every claim id a review cites must belong to the target it names.
A reply that mislabels a target, skips one, or cites a claim the reviewer was never shown is a
validation error, so it earns the one repair and then becomes an absence.

Drafts are as strict as the contracts: frozen, unknown fields rejected, no field for private
reasoning.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping

from pydantic import Field

from velune.council.contracts import (
    ClaimId,
    Confidence,
    Critique,
    Disagreement,
    DisagreementKind,
    MaybeText240,
    Severity,
    Slug,
    Text240,
    Text400,
)
from velune.council.profiles import RoleProfile, SeatSpec
from velune.council.serialization import Contract

MAX_REVIEWS = 5
MAX_AGREEMENTS = 8
MAX_OBJECTIONS = 6

_CLAIM_REF = re.compile(r"\b([A-Z]{2,3})-\d{1,3}\b")


class ObjectionDraft(Contract):
    claim_id: ClaimId
    objection: Text240
    kind: DisagreementKind
    severity: Severity
    suggested_resolution: MaybeText240 = ""


class ReviewDraft(Contract):
    target: Slug
    steelman: Text400
    agreements: tuple[ClaimId, ...] = Field(default=(), max_length=MAX_AGREEMENTS)
    disagreements: tuple[ObjectionDraft, ...] = Field(default=(), max_length=MAX_OBJECTIONS)
    no_material_issues: bool = False
    confidence: Confidence


class ReviewReplyDraft(Contract):
    reviews: tuple[ReviewDraft, ...] = Field(min_length=1, max_length=MAX_REVIEWS)


def foreign_claim_ids(
    texts: Iterable[str], *, allowed: Collection[str], known: Collection[str]
) -> list[str]:
    """Claim ids in free text whose prefix belongs to a seat that text may not mention.

    ``known`` is every claim prefix in the profile; ``allowed`` the ones this text may use. Other
    text that merely looks like an id (``ISO-9``) is not a seat's and is left alone.
    """
    found: list[str] = []
    for text in texts:
        for match in _CLAIM_REF.finditer(text):
            prefix = match.group(1)
            if prefix in known and prefix not in allowed:
                found.append(match.group(0))
    return found


def profile_prefixes(profile: RoleProfile) -> frozenset[str]:
    return frozenset(spec.claim_prefix for spec in profile.all_seats)


def critiques_from_draft(
    draft: ReviewReplyDraft,
    *,
    reviewer: SeatSpec,
    shown: Mapping[str, frozenset[str]],
    profile: RoleProfile,
) -> tuple[Critique, ...]:
    """Build one ``Critique`` per assigned target, or raise ``ValueError`` saying what is wrong.

    ``shown`` maps each target the reviewer was given to the claim ids it was shown for that
    target. The reply must cover exactly those targets, once each.
    """
    targets = [review.target for review in draft.reviews]
    if len(set(targets)) != len(targets):
        raise ValueError("reviews: each target may be reviewed only once")
    if set(targets) != set(shown):
        raise ValueError(
            "reviews: expected exactly one review for each of " + ", ".join(sorted(shown))
        )
    known = profile_prefixes(profile)
    order = {spec.id: index for index, spec in enumerate(profile.perspective_seats)}
    critiques: list[Critique] = []
    for index, review in enumerate(draft.reviews):
        where = f"reviews.{index}"
        cited = [*review.agreements, *(item.claim_id for item in review.disagreements)]
        for claim_id in cited:
            if claim_id not in shown[review.target]:
                raise ValueError(f"{where}: claim {claim_id} does not belong to {review.target}")
        if review.no_material_issues and review.disagreements:
            raise ValueError(f"{where}: no_material_issues cannot come with disagreements")
        if not review.no_material_issues and not review.disagreements:
            raise ValueError(f"{where}: give a disagreement or set no_material_issues")
        target_prefix = profile.seat(review.target).claim_prefix
        texts = [
            review.steelman,
            *(item.objection for item in review.disagreements),
            *(item.suggested_resolution for item in review.disagreements),
        ]
        stray = foreign_claim_ids(
            texts, allowed={target_prefix, reviewer.claim_prefix}, known=known
        )
        if stray:
            raise ValueError(f"{where}: text cites {stray[0]}, which is not this target's claim")
        critiques.append(
            Critique(
                reviewer_seat=reviewer.id,
                target_seat=review.target,
                steelman=review.steelman,
                agreements=review.agreements,
                disagreements=tuple(
                    Disagreement(
                        claim_id=item.claim_id,
                        objection=item.objection,
                        kind=item.kind,
                        severity=item.severity,
                        suggested_resolution=item.suggested_resolution,
                    )
                    for item in review.disagreements
                ),
                no_material_issues=review.no_material_issues,
                confidence=review.confidence,
            )
        )
    critiques.sort(key=lambda item: order[item.target_seat])
    return tuple(critiques)
