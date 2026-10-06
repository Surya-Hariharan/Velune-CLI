"""Profiles and the registry: validation, GENERAL's exact seat set, declared-only domains."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from velune.council.domain import (
    STAGE_ORDER,
    CouncilDomain,
    QuorumRule,
    SeatKind,
    StageId,
)
from velune.council.profiles import (
    BUILTIN_PROFILES,
    GENERAL_PROFILE,
    ProfileNotRunnable,
    ProfileRegistry,
    RoleProfile,
    RoutingRole,
    SeatSpec,
    UnknownProfile,
    default_registry,
)


def _seat(seat_id: str, prefix: str, kind: SeatKind = SeatKind.PERSPECTIVE) -> SeatSpec:
    return SeatSpec(
        id=seat_id,
        kind=kind,
        display_name=seat_id.title(),
        objective="x",
        claim_prefix=prefix,
        routing_role=RoutingRole.REVIEWER,
    )


def _profile(**overrides) -> RoleProfile:
    fields = {
        "id": "demo",
        "domain": CouncilDomain.GENERAL,
        "perspective_seats": (_seat("aa", "AA"), _seat("bb", "BB"), _seat("cc", "CC")),
        "moderator": _seat("mod", "MO", SeatKind.MODERATOR),
        "arbitrator": _seat("arb", "AR", SeatKind.ARBITRATOR),
        "synthesizer": _seat("syn", "SY", SeatKind.SYNTHESIZER),
        "default_quorum": QuorumRule(min_ok=2),
    }
    fields.update(overrides)
    return RoleProfile(**fields)


def test_general_perspectives_are_exactly_the_locked_five():
    assert [s.id for s in GENERAL_PROFILE.perspective_seats] == [
        "analyst",
        "skeptic",
        "creative",
        "fact_checker",
        "practicalist",
    ]
    assert all(s.kind is SeatKind.PERSPECTIVE for s in GENERAL_PROFILE.perspective_seats)


def test_orchestration_seats_are_not_perspectives():
    perspective_ids = {s.id for s in GENERAL_PROFILE.perspective_seats}
    assert {s.id for s in GENERAL_PROFILE.orchestration_seats} == {
        "moderator",
        "arbitrator",
        "synthesizer",
    }
    assert not perspective_ids & {s.id for s in GENERAL_PROFILE.orchestration_seats}
    assert GENERAL_PROFILE.moderator.kind is SeatKind.MODERATOR
    assert GENERAL_PROFILE.arbitrator.kind is SeatKind.ARBITRATOR
    assert GENERAL_PROFILE.synthesizer.kind is SeatKind.SYNTHESIZER


def test_general_claim_prefixes_and_routing_hints():
    prefixes = {s.id: s.claim_prefix for s in GENERAL_PROFILE.all_seats}
    assert prefixes == {
        "analyst": "AN",
        "skeptic": "SK",
        "creative": "CR",
        "fact_checker": "FC",
        "practicalist": "PR",
        "moderator": "MO",
        "arbitrator": "AR",
        "synthesizer": "SY",
    }
    hints = {s.id: s.routing_role for s in GENERAL_PROFILE.all_seats}
    assert hints["skeptic"] is RoutingRole.CHALLENGER
    assert hints["synthesizer"] is RoutingRole.SYNTHESIZER
    keys = {s.id: s.prompt_key for s in GENERAL_PROFILE.all_seats}
    # only the seats a stage prompts point at the prompt library; never prompt text itself
    assert keys["analyst"] == "council.general.analyst"
    assert keys["moderator"] == "council.general.moderator"
    assert keys["arbitrator"] is None and keys["synthesizer"] is None
    for profile in BUILTIN_PROFILES:
        if profile is not GENERAL_PROFILE:
            assert all(s.prompt_key is None for s in profile.all_seats)


@pytest.mark.parametrize("profile", BUILTIN_PROFILES, ids=lambda p: p.id)
def test_every_builtin_profile_validates_under_the_same_rules(profile):
    ids = [s.id for s in profile.all_seats]
    assert len(set(ids)) == len(ids)
    assert 3 <= len(profile.perspective_seats) <= 7
    assert profile.runnable is (profile.domain is CouncilDomain.GENERAL)


def test_declared_domains_are_not_runnable():
    registry = default_registry()
    for profile_id in ("coding", "teaching", "brainstorming"):
        assert registry.get(profile_id).runnable is False
        with pytest.raises(ProfileNotRunnable):
            registry.require_runnable(profile_id)
    assert registry.require_runnable("general") is GENERAL_PROFILE


def test_registry_rejects_duplicates_and_unknown_ids():
    registry = ProfileRegistry((GENERAL_PROFILE,))
    with pytest.raises(ValueError):
        registry.register(GENERAL_PROFILE)
    with pytest.raises(UnknownProfile):
        registry.get("nope")
    assert registry.ids() == ("general",)


def test_registries_do_not_share_state():
    a, b = default_registry(), default_registry()
    a.register(_profile())
    assert "demo" in a.ids() and "demo" not in b.ids()


def test_valid_custom_profile_builds():
    assert _profile().seat("aa").claim_prefix == "AA"


@pytest.mark.parametrize(
    "overrides",
    [
        {"perspective_seats": (_seat("aa", "AA"), _seat("bb", "BB"))},  # too few
        {
            "perspective_seats": tuple(_seat(f"s{i}", f"S{chr(65 + i)}") for i in range(8))
        },  # too many
        {"perspective_seats": (_seat("aa", "AA"), _seat("aa", "BB"), _seat("cc", "CC"))},
        {"perspective_seats": (_seat("aa", "AA"), _seat("bb", "AA"), _seat("cc", "CC"))},
        {"moderator": _seat("mod", "MO", SeatKind.ARBITRATOR)},
        {"synthesizer": _seat("syn", "SY", SeatKind.PERSPECTIVE)},
        {"perspective_seats": (_seat("aa", "AA"), _seat("bb", "BB"), _seat("mod", "CC"))},
        {"default_quorum": QuorumRule(min_ok=9)},
        {"default_quorum": QuorumRule(min_ok=1, require_any_of=("ghost",))},
        {"default_quorum": QuorumRule(min_ok=1, require_any_of=("mod",))},
        {"id": "Bad Id"},
    ],
)
def test_invalid_profiles_are_rejected(overrides):
    with pytest.raises(ValidationError):
        _profile(**overrides)


@pytest.mark.parametrize("seat_id", ["Bad", "1abc", "has space", "a", "x" * 40])
def test_seat_ids_must_be_slugs(seat_id):
    with pytest.raises(ValidationError):
        _seat(seat_id, "AA")


@pytest.mark.parametrize("prefix", ["a", "AAAA", "A1", "ab"])
def test_claim_prefix_must_be_two_or_three_capitals(prefix):
    with pytest.raises(ValidationError):
        _seat("good", prefix)


def test_profiles_are_frozen_and_reject_unknown_fields():
    with pytest.raises(ValidationError):
        GENERAL_PROFILE.runnable = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SeatSpec(
            id="ok",
            kind=SeatKind.PERSPECTIVE,
            display_name="Ok",
            objective="x",
            claim_prefix="OK",
            routing_role=RoutingRole.PLANNER,
            reasoning="hidden thoughts",
        )


def test_unknown_seat_lookup_raises_keyerror():
    with pytest.raises(KeyError):
        GENERAL_PROFILE.seat("ghost")


def test_stage_order_is_r0_to_r5():
    assert [s.value for s in STAGE_ORDER] == [
        "frame",
        "perspectives",
        "review",
        "revision",
        "arbitration",
        "synthesis",
    ]
    assert [s.round for s in STAGE_ORDER] == [0, 1, 2, 3, 4, 5]
    assert StageId.SYNTHESIS.round == 5


def test_routing_role_mirrors_the_legacy_council_role():
    """The core mirrors the legacy enum by value so it never has to import the provider stack."""
    from velune.models.specializations import CouncilRole

    assert {r.value for r in RoutingRole} == {r.value for r in CouncilRole}
