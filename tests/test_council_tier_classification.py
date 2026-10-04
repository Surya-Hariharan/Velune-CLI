"""C1 regression tests: tier classification must not depend on fragile
substring matching, and model speed must never escalate complexity."""

from velune.cognition.council.tiers import CouncilTier, classify_task_tier


def test_typo_phrasing_variants_all_map_to_minimal():
    variants = [
        "fix typo",
        "fix the typo",
        "fix a typo",
        "fix this typo",
    ]
    for prompt in variants:
        assert classify_task_tier(prompt, repo_context="") == CouncilTier.MINIMAL, prompt


def test_fast_model_does_not_escalate_simple_prompt_to_full():
    # Previously: available_tps > 40 unconditionally escalated to FULL.
    tier = classify_task_tier(
        "say hello",
        repo_context="",
        available_tps=100.0,
    )
    assert tier != CouncilTier.FULL


def test_fast_model_only_lowers_bar_for_short_prompts():
    tier = classify_task_tier(
        "do the thing",
        repo_context="",
        available_tps=25.0,
    )
    assert tier == CouncilTier.MINIMAL


def test_explicit_full_signal_still_escalates():
    tier = classify_task_tier(
        "refactor the authentication module across all files",
        repo_context="",
        available_tps=100.0,
    )
    assert tier == CouncilTier.FULL


def test_intent_hint_pins_instant_for_short_question():
    tier = classify_task_tier(
        "who wrote this",
        repo_context="",
        intent_hint="question",
    )
    assert tier == CouncilTier.INSTANT


def test_intent_hint_floors_full_for_security():
    tier = classify_task_tier(
        "look at this code",
        repo_context="",
        intent_hint="security",
        available_tps=100.0,
    )
    assert tier == CouncilTier.FULL


def test_hello_is_instant_with_question_intent_hint():
    # Mirrors what the orchestrator actually does: IntentClassifier resolves
    # "hello" to QUESTION (no keyword signal, low confidence fallback), and
    # that hint is what pins the tier to INSTANT rather than the default
    # STANDARD fallback a bare keyword scan would otherwise reach.
    assert (
        classify_task_tier("hello", repo_context="", intent_hint="question")
        == CouncilTier.INSTANT
    )
