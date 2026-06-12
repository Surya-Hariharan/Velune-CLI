"""Tests for council tier classification heuristics."""

from velune.cognition.council.tiers import CouncilTier, classify_task_tier


def test_classify_instant_tier():
    # "explain" is an instant_signal; word_count=5 < 20
    tier = classify_task_tier(
        prompt="explain what this function does",
        repo_context="",
    )
    assert tier == CouncilTier.INSTANT


def test_classify_full_tier():
    # "refactor" is a full_signal
    tier = classify_task_tier(
        prompt="refactor the database layer for concurrency",
        repo_context="",
    )
    assert tier == CouncilTier.FULL


def test_classify_standard_tier():
    # No special signals match; falls through to STANDARD
    tier = classify_task_tier(
        prompt="fix the null pointer bug in auth.ts",
        repo_context="",
    )
    assert tier == CouncilTier.STANDARD
