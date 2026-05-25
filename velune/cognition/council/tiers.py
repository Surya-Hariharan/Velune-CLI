from enum import Enum


class CouncilTier(str, Enum):
    INSTANT = "instant"     # Coder only, no review. Read-only queries, explanations.
    STANDARD = "standard"   # Coder + Reviewer. Small edits, bug fixes.
    FULL = "full"           # All agents. Architecture changes, multi-file edits.

def classify_task_tier(
    prompt: str,
    repo_context: str,
    available_tps: float = 8.0,  # tokens per second for available models
) -> CouncilTier:
    """Classify task complexity to select appropriate council tier."""

    prompt_lower = prompt.lower()
    word_count = len(prompt.split())

    # INSTANT: read-only, explanation, or trivial
    instant_signals = ["explain", "what is", "how does", "show me", "list", "describe"]
    if any(s in prompt_lower for s in instant_signals) and word_count < 20:
        return CouncilTier.INSTANT

    # FULL: architectural, multi-file, security, concurrency
    full_signals = [
        "refactor", "redesign", "architect", "migrate",
        "security", "concurrent", "async", "database schema",
        "multiple files", "all files",
    ]
    if any(s in prompt_lower for s in full_signals):
        return CouncilTier.FULL

    # FULL: if model is fast enough, full council is affordable
    if available_tps > 40.0:  # GPU-accelerated, fast local model
        return CouncilTier.FULL

    # Otherwise: standard (Coder + Reviewer)
    return CouncilTier.STANDARD
