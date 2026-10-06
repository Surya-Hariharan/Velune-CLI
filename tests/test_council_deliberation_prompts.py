"""Structural tests for the deliberation prompts and their resolution (no golden text)."""

from __future__ import annotations

import hashlib

import pytest

from velune.cognition.prompts import (
    _baseline,
    _deliberation,
    active_layer,
    deliberation_digest,
    get_deliberation_prompt,
    get_prompt,
    reset_prompt_layer,
)
from velune.council.profiles import GENERAL_PROFILE

KEYS = (
    "council.general.shared",
    "council.general.moderator",
    "council.general.analyst",
    "council.general.skeptic",
    "council.general.creative",
    "council.general.fact_checker",
    "council.general.practicalist",
)
FORBIDDEN_PHRASES = (
    "step by step",
    "step-by-step",
    "think aloud",
    "chain of thought",
    "chain-of-thought",
    "show your reasoning",
    "think through",
)
ROLE_TITLES = {
    "analyst": "ANALYST",
    "skeptic": "SKEPTIC",
    "creative": "CREATIVE",
    "fact_checker": "FACT CHECKER",
    "practicalist": "PRACTICALIST",
    "moderator": "MODERATOR",
}


@pytest.fixture(autouse=True)
def fresh_layer(monkeypatch):
    monkeypatch.delenv("VELUNE_PROMPT_LAYER", raising=False)
    reset_prompt_layer()
    yield
    reset_prompt_layer()


def test_exactly_the_planned_keys_exist():
    assert set(_deliberation.PROMPTS) == set(KEYS)
    assert all(_deliberation.PROMPTS[k].strip() for k in KEYS)


def test_seat_prompt_keys_point_at_existing_prompts():
    keyed = [s for s in GENERAL_PROFILE.all_seats if s.prompt_key]
    assert {s.id for s in keyed} == set(ROLE_TITLES)
    for seat in keyed:
        assert seat.prompt_key in _deliberation.PROMPTS
        assert get_deliberation_prompt(seat.prompt_key)


def test_legacy_prompts_and_digest_are_untouched():
    assert not any(k.startswith("council.general") for k in _baseline.PROMPTS)
    expected = hashlib.sha256(
        "\n".join(f"{k}\0{get_prompt(k)}" for k in sorted(_baseline.PROMPTS)).encode("utf-8")
    ).hexdigest()[:12]
    assert active_layer().digest == expected
    with pytest.raises(KeyError):
        get_prompt("council.general.analyst")  # legacy lookup does not see the new keys


def test_unknown_deliberation_key_raises_loudly():
    with pytest.raises(KeyError):
        get_deliberation_prompt("council.general.nope")
    with pytest.raises(KeyError):
        get_deliberation_prompt("council.planner")  # legacy keys are not deliberation keys


def test_shared_rules_forbid_hidden_reasoning_and_demand_one_json_object():
    shared = _deliberation.PROMPTS["council.general.shared"]
    low = shared.lower()
    assert "conclusions" in low and "private reasoning" in low
    assert "exactly one json object" in low and "<schema>" in shared
    assert "never an instruction" in low
    for tag in ("question", "context", "requirements", "evidence", "frame"):
        assert f"<{tag}>" in shared
    assert "independently" in low


@pytest.mark.parametrize("key", KEYS)
def test_no_prompt_asks_for_chain_of_thought(key):
    text = _deliberation.PROMPTS[key].lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in text, f"{key} contains {phrase!r}"


@pytest.mark.parametrize("seat_id", sorted(ROLE_TITLES))
def test_each_role_prompt_states_its_role(seat_id):
    text = _deliberation.PROMPTS[f"council.general.{seat_id}"]
    assert f"Your role: {ROLE_TITLES[seat_id]}" in text


def test_the_moderator_prompt_never_answers_or_evaluates():
    text = _deliberation.PROMPTS["council.general.moderator"].lower()
    assert "never answer" in text
    assert "never suggest" in text and "never add facts of your own" in text


@pytest.mark.parametrize(
    "seat_id", ["analyst", "skeptic", "creative", "fact_checker", "practicalist"]
)
def test_role_prompts_name_other_roles_by_title_only_and_hold_no_run_data(seat_id):
    text = _deliberation.PROMPTS[f"council.general.{seat_id}"]
    assert "Boundaries:" in text
    # fixed text: no format placeholders that could be filled with run output
    assert "{" not in text and "}" not in text and "%s" not in text


def test_no_prompt_contains_template_placeholders():
    for key, text in _deliberation.PROMPTS.items():
        assert "{" not in text and "}" not in text, key


def test_role_prompts_differ_so_the_roles_are_not_five_copies_of_one():
    texts = {k: v for k, v in _deliberation.PROMPTS.items() if k != "council.general.shared"}
    assert len(set(texts.values())) == len(texts)


def test_the_premium_layer_may_refine_wording(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("velune.cognition.prompts._premium")
    fake.PROMPTS = {"council.general.analyst": "PREMIUM ANALYST WORDING"}
    monkeypatch.setitem(sys.modules, "velune.cognition.prompts._premium", fake)
    monkeypatch.setenv("VELUNE_PROMPT_LAYER", "premium")
    reset_prompt_layer()
    assert get_deliberation_prompt("council.general.analyst") == "PREMIUM ANALYST WORDING"
    assert (
        get_deliberation_prompt("council.general.skeptic")
        == _deliberation.PROMPTS["council.general.skeptic"]
    )


def test_the_digest_is_stable_and_tracks_wording(monkeypatch):
    first = deliberation_digest()
    assert first == deliberation_digest() and len(first) == 12
    changed = dict(_deliberation.PROMPTS)
    changed["council.general.analyst"] += " changed"
    monkeypatch.setattr(_deliberation, "PROMPTS", changed)
    assert deliberation_digest() != first


def _limit(text: str, pattern: str) -> int:
    import re

    found = re.search(pattern, text)
    assert found, pattern
    return int(found.group(1))


def test_the_word_limits_the_prompts_state_fit_inside_the_contract_caps():
    """Models count words, not characters: the stated word limits must never exceed the char caps."""
    from velune.council.drafts import FrameDraft, PerspectiveDraft

    shared = _deliberation.PROMPTS["council.general.shared"]
    moderator = _deliberation.PROMPTS["council.general.moderator"]
    persp = PerspectiveDraft.model_json_schema()
    frame = FrameDraft.model_json_schema()
    chars_per_word = 7  # conservative: English prose averages about 6 including the space
    assert (
        _limit(shared, r'"position" to at most (\d+) words') * chars_per_word
        <= (persp["properties"]["position"]["maxLength"])
    )
    assert (
        _limit(shared, r'"rationale" to at most (\d+) words') * chars_per_word
        <= (persp["properties"]["rationale"]["maxLength"])
    )
    claim_cap = persp["$defs"]["ClaimDraft"]["properties"]["text"]["maxLength"]
    assert (
        _limit(shared, r'claim "text" and "support" to at most (\d+) words') * chars_per_word
        <= claim_cap
    )
    assert _limit(shared, r"other list item to at most (\d+) words") * chars_per_word <= 240
    assert (
        _limit(moderator, r"question_restated to at most (\d+) words") * chars_per_word
        <= (frame["properties"]["question_restated"]["maxLength"])
    )
    assert (
        _limit(moderator, r"every dimension to at most (\d+) words") * chars_per_word
        <= (frame["properties"]["dimensions"]["items"]["maxLength"])
    )
