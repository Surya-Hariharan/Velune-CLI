import re
from typing import Any

from velune._compat import StrEnum


class CouncilTier(StrEnum):
    INSTANT = "instant"  # Coder only, no review. Read-only queries, explanations.
    MINIMAL = "minimal"  # Planner + Coder, no Reviewer. Simple bug fixes on fast hardware.
    STANDARD = "standard"  # Coder + Reviewer. Small edits, bug fixes.
    FULL = "full"  # All agents. Architecture changes, multi-file edits.


# Intent categories (from velune.cognition.intent.IntentType) that pin a tier
# floor/ceiling regardless of keyword-signal ambiguity. Keeping the mapping
# here (rather than importing IntentType) avoids a hard dependency between
# the two classifiers while still letting the orchestrator hand this module
# one canonical intent judgement instead of re-deriving complexity from
# scratch. See velune.cognition.council.contracts module docstring for why
# tier composition must not silently drift from a single source of truth —
# the same principle applies to *which* tier gets selected in the first place.
_INSTANT_INTENTS = frozenset({"explain", "question", "documentation", "search"})
_FULL_FLOOR_INTENTS = frozenset({"security", "architecture", "dependency_analysis"})

# Signal patterns are matched against a normalized prompt (lowercased,
# whitespace-collapsed) using word boundaries and tolerant articles, so
# "fix typo", "fix the typo", and "fix a typo" all map the same way instead
# of depending on an exact substring.
_INSTANT_PATTERN = re.compile(
    r"\b(explain|what\s+is|what\s+are|how\s+does|how\s+do|show\s+me|list|describe)\b"
)
_MINIMAL_PATTERN = re.compile(
    r"\bfix\s+(?:the\s+|a\s+|this\s+)?typo\b"
    r"|\btweak\b|\bsimple\s+change\b|\bcomment\b|\bformat\b"
)
_FULL_PATTERN = re.compile(
    r"\brefactor\b|\bredesign\b|\barchitect(?:ure)?\b|\bmigrat(?:e|ion|ing)\b"
    r"|\bsecurity\b|\bconcurren(?:t|cy)\b|\basync\b"
    r"|\bdatabase\s+schema\b|\bmultiple\s+files\b|\ball\s+files\b"
)


def _normalize(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.strip().lower())


def classify_task_tier(
    prompt: str,
    repo_context: str,
    available_tps: float = 8.0,  # tokens per second for available models
    max_council_tier: str | None = None,
    default_tier_override: str | None = None,
    queue_depth: int = 0,
    intent_hint: str | None = None,
) -> CouncilTier:
    """Classify task complexity to select appropriate council tier.

    ``intent_hint`` is the canonical ``IntentType`` value (lowercase) already
    computed by ``velune.cognition.intent.IntentClassifier`` for this prompt,
    when the caller has one available. It disambiguates cases the keyword
    signals below are inherently fragile on, without this module re-running
    its own separate intent inference.
    """

    # 1. Handle explicit default override
    if default_tier_override and default_tier_override != "auto":
        try:
            tier = CouncilTier(default_tier_override.lower())
            return _apply_ceiling(tier, max_council_tier)
        except ValueError:
            pass

    normalized = _normalize(prompt)
    word_count = len(normalized.split())

    # 2. Heuristics with queue depth checks to avoid CPU starvation
    # INSTANT: read-only, explanation, or trivial
    if word_count < 20 and (intent_hint in _INSTANT_INTENTS or _INSTANT_PATTERN.search(normalized)):
        return _apply_ceiling(CouncilTier.INSTANT, max_council_tier)

    # MINIMAL: simple bug fixes, typos, comment edits
    if word_count < 15 and _MINIMAL_PATTERN.search(normalized):
        return _apply_ceiling(CouncilTier.MINIMAL, max_council_tier)

    # FULL: architectural, multi-file, security, concurrency, dependency-impact
    if intent_hint in _FULL_FLOOR_INTENTS or _FULL_PATTERN.search(normalized):
        if queue_depth > 2:
            classified = CouncilTier.STANDARD
        else:
            classified = CouncilTier.FULL
        return _apply_ceiling(classified, max_council_tier)

    # Model speed is a latency/affordability signal, not a complexity signal:
    # a fast model makes deeper reasoning *cheaper*, so it can only lower the
    # bar to MINIMAL for already-short prompts. It must never escalate a task
    # towards FULL by itself — that was the C1 fallback bug (fast model implied
    # maximum orchestration, backwards from what capability should buy you).
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
        intent_hint: str | None = None,
    ) -> CouncilTier:
        """Determine task complexity and resource consumption tier policy.

        ``intent_hint`` is the canonical intent category (lowercase
        ``IntentType`` value) for *prompt*, if the caller already computed
        one — see :func:`classify_task_tier`.
        """
        queue_depth = self.get_queue_depth()

        tier = classify_task_tier(
            prompt=prompt,
            repo_context=repo_context,
            available_tps=available_tps,
            max_council_tier=self.max_council_tier,
            default_tier_override=self.default_tier_override,
            queue_depth=queue_depth,
            intent_hint=intent_hint,
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
