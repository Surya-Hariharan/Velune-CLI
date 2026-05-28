from enum import Enum


class CouncilTier(str, Enum):
    INSTANT = "instant"     # Coder only, no review. Read-only queries, explanations.
    MINIMAL = "minimal"     # Planner + Coder, no Reviewer. Simple bug fixes on fast hardware.
    STANDARD = "standard"   # Coder + Reviewer. Small edits, bug fixes.
    FULL = "full"           # All agents. Architecture changes, multi-file edits.


def classify_task_tier(
    prompt: str,
    repo_context: str,
    available_tps: float = 8.0,  # tokens per second for available models
    max_council_tier: str | None = None,
    default_tier_override: str | None = None,
    queue_depth: int = 0,
) -> CouncilTier:
    """Classify task complexity to select appropriate council tier."""

    # 1. Handle explicit default override
    if default_tier_override and default_tier_override != "auto":
        try:
            tier = CouncilTier(default_tier_override.lower())
            return _apply_ceiling(tier, max_council_tier)
        except ValueError:
            pass

    prompt_lower = prompt.lower()
    word_count = len(prompt.split())

    # 2. Heuristics with queue depth checks to avoid CPU starvation
    # INSTANT: read-only, explanation, or trivial
    instant_signals = ["explain", "what is", "how does", "show me", "list", "describe"]
    if any(s in prompt_lower for s in instant_signals) and word_count < 20:
        return _apply_ceiling(CouncilTier.INSTANT, max_council_tier)

    # MINIMAL: simple bug fixes, typos, comment edits
    minimal_signals = ["fix typo", "tweak", "simple change", "comment", "format"]
    if any(s in prompt_lower for s in minimal_signals) and word_count < 15:
        return _apply_ceiling(CouncilTier.MINIMAL, max_council_tier)

    # FULL: architectural, multi-file, security, concurrency
    full_signals = [
        "refactor", "redesign", "architect", "migrate",
        "security", "concurrent", "async", "database schema",
        "multiple files", "all files",
    ]
    if any(s in prompt_lower for s in full_signals):
        if queue_depth > 2:
            classified = CouncilTier.STANDARD
        else:
            classified = CouncilTier.FULL
        return _apply_ceiling(classified, max_council_tier)

    # FULL: if model is fast enough, and queue depth is very low, FULL council is affordable
    if available_tps > 40.0:  # GPU-accelerated, fast local model
        if queue_depth > 1:
            classified = CouncilTier.STANDARD
        else:
            classified = CouncilTier.FULL
        return _apply_ceiling(classified, max_council_tier)

    # MINIMAL for simple bug fixes on fast hardware
    if available_tps > 20.0 and word_count < 10:
        return _apply_ceiling(CouncilTier.MINIMAL, max_council_tier)

    # Otherwise: standard (Coder + Reviewer)
    return _apply_ceiling(CouncilTier.STANDARD, max_council_tier)


def _apply_ceiling(tier: CouncilTier, max_tier_str: str | None) -> CouncilTier:
    if not max_tier_str:
        return tier
    try:
        max_tier = CouncilTier(max_tier_str.lower())
    except ValueError:
        return tier

    # Order: INSTANT < MINIMAL < STANDARD < FULL
    order = {
        CouncilTier.INSTANT: 0,
        CouncilTier.MINIMAL: 1,
        CouncilTier.STANDARD: 2,
        CouncilTier.FULL: 3,
    }
    if order[tier] > order[max_tier]:
        return max_tier
    return tier
