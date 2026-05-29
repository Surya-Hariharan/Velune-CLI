from enum import Enum
from typing import Any


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


class TierClassifier:
    """Centralizes council tier decision-making and resource-aware classification policies."""

    def __init__(
        self,
        task_registry: Any | None = None,
        max_council_tier: str = "full",
        default_tier_override: str = "auto",
        low_resource_mode: bool = False,
    ) -> None:
        self.task_registry = task_registry
        self.max_council_tier = max_council_tier
        self.default_tier_override = default_tier_override
        self.low_resource_mode = low_resource_mode

    def get_queue_depth(self) -> int:
        """Resolve queue depth from the task registry safely without direct locator calls in main methods."""
        if self.task_registry and hasattr(self.task_registry, "pending_count"):
            try:
                return self.task_registry.pending_count()
            except Exception:
                pass
        return 0

    def classify(
        self,
        prompt: str,
        repo_context: str,
        available_tps: float = 8.0,
    ) -> CouncilTier:
        """Determine task complexity and resource consumption tier policy."""
        queue_depth = self.get_queue_depth()
        
        tier = classify_task_tier(
            prompt=prompt,
            repo_context=repo_context,
            available_tps=available_tps,
            max_council_tier=self.max_council_tier,
            default_tier_override=self.default_tier_override,
            queue_depth=queue_depth,
        )

        # Apply structural fan-in tier escalation floor
        floor = CouncilTier.INSTANT
        max_fan_in = 0
        try:
            import re
            from velune.kernel.registry import get_container
            container = get_container()
            if container.has("runtime.repository_cognition"):
                repo_service = container.get("runtime.repository_cognition")
                grapher = repo_service.grapher
                
                # Scan prompt for mentioned source files
                mentioned_files = re.findall(r"[\w\/\.\-]+\.(?:py|js|ts|go|rs)", prompt)
                for mf in mentioned_files:
                    dependents = grapher.get_dependents(mf)
                    fan_in = len(dependents)
                    if fan_in > max_fan_in:
                        max_fan_in = fan_in
        except Exception:
            pass

        if max_fan_in >= 5:
            floor = CouncilTier.FULL
        elif max_fan_in >= 3:
            floor = CouncilTier.STANDARD
        elif max_fan_in >= 1:
            floor = CouncilTier.MINIMAL

        # Structural escalation only upgrades, never downgrades keyword decisions
        order = {
            CouncilTier.INSTANT: 0,
            CouncilTier.MINIMAL: 1,
            CouncilTier.STANDARD: 2,
            CouncilTier.FULL: 3,
        }

        if order[floor] > order[tier]:
            tier = floor
            # Apply ceiling after upgrading
            tier = _apply_ceiling(tier, self.max_council_tier)

        if self.low_resource_mode and tier == CouncilTier.FULL:
            tier = CouncilTier.STANDARD

        return tier

