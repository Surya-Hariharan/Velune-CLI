"""Who reviews whom in R2, resolved from a profile's review graph.

The graph is profile data (``RoleProfile.review_graph``). This module turns it into the
``AssignmentSource`` the runner hands to ``StageView``, so the topology is enforced by the same read
path as every other visibility rule: a reviewer can read only its own perspective and the peers the
graph names, and take a claims digest of only the authors the graph audits.

Nothing here is dynamic. A peer that did not deliver simply has no artifact to read; no one is
rerouted to take its place.
"""

from __future__ import annotations

from velune.council.domain import StageId
from velune.council.profiles import RoleProfile


class ProfileAssignments:
    """``DigestAssignmentSource`` backed by a profile's review graph (R2 only)."""

    def __init__(self, profile: RoleProfile) -> None:
        self._profile = profile

    def assigned_authors(self, stage: StageId, viewer: str) -> tuple[str, ...]:
        if stage is not StageId.REVIEW:
            return ()
        item = self._profile.review_for(viewer)
        return item.full if item else ()

    def digest_authors(self, stage: StageId, viewer: str) -> tuple[str, ...] | None:
        if stage is not StageId.REVIEW:
            return None  # other stages keep their contract-level digest rules
        item = self._profile.review_for(viewer)
        return item.digest if item else ()


def reviewers_of(profile: RoleProfile, target: str) -> tuple[str, ...]:
    """Every seat that reviews ``target`` (in full or by digest), in profile order."""
    found = []
    for seat in profile.perspective_seats:
        item = profile.review_for(seat.id)
        if item is not None and target in item.targets:
            found.append(seat.id)
    return tuple(found)


def coverage_after_failures(profile: RoleProfile, failed: frozenset[str]) -> dict[str, int]:
    """How many reviewers each seat keeps if the ``failed`` reviewers deliver nothing."""
    return {
        seat.id: len([r for r in reviewers_of(profile, seat.id) if r not in failed])
        for seat in profile.perspective_seats
    }
