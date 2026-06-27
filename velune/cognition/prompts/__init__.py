"""Central system-prompt library for Velune's council agents and chat surfaces.

Every LLM-facing system prompt in Velune is resolved through this package so the
wording lives in exactly one place and can be tuned without touching agent code.

Two layers are supported:

* ``_baseline`` — committed, public-safe prompts. Always present. Guarantees the
  product runs correctly on a fresh clone even if the private layer is absent.
* ``_premium`` — an **optional, git-ignored** module holding the high-signal
  "house" prompts. When present, its entries override the baseline key-for-key.
  This lets the tuned prompts ship with a build without being committed to the
  public history. See ``_premium.example.py`` for the contract.

Both layers expose a single ``PROMPTS: dict[str, str]`` mapping. Resolution is
``premium.get(key, baseline[key])`` — premium wins, baseline backstops, and a
missing key in *both* raises loudly at import-resolve time rather than silently
sending an empty system prompt to a model.

Prompt keys are namespaced ``"<surface>.<role>"`` (e.g. ``"council.planner"``,
``"chat.interactive"``). Use the module-level constants below rather than raw
strings so typos fail fast.
"""

from __future__ import annotations

from velune.cognition.prompts import _baseline

# ── Stable prompt keys ───────────────────────────────────────────────────────
# Council deliberation seats.
COUNCIL_PLANNER = "council.planner"
COUNCIL_CODER = "council.coder"
COUNCIL_REVIEWER = "council.reviewer"
COUNCIL_CHALLENGER = "council.challenger"
COUNCIL_SYNTHESIZER = "council.synthesizer"

# Direct chat surfaces.
CHAT_INTERACTIVE = "chat.interactive"  # REPL main loop (velune, no args)
CHAT_CONVERSATIONAL = "chat.conversational"  # `velune chat` low-latency mode

_OVERRIDES: dict[str, str] = {}


def _load_premium_overrides() -> dict[str, str]:
    """Import the optional, git-ignored premium prompt layer if it exists.

    Failure to import (module absent, or malformed) is non-fatal: the baseline
    layer always provides a working prompt. We only log at debug level so a
    missing private file never disrupts a developer running from a clean clone.
    """
    try:
        from velune.cognition.prompts import _premium  # type: ignore[attr-defined]
    except Exception:
        return {}

    overrides = getattr(_premium, "PROMPTS", None)
    if not isinstance(overrides, dict):
        return {}
    # Keep only str→str entries; ignore anything malformed defensively.
    return {
        k: v
        for k, v in overrides.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


_OVERRIDES = _load_premium_overrides()


def get_prompt(key: str) -> str:
    """Resolve a system prompt by key.

    Premium overrides take precedence over the committed baseline. Raises
    ``KeyError`` if the key is unknown in *both* layers — that is a programming
    error (a typo'd key), never something to paper over with an empty prompt.
    """
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    try:
        return _baseline.PROMPTS[key]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(
            f"Unknown system-prompt key {key!r}. Known keys: {sorted(_baseline.PROMPTS)}"
        ) from exc


def is_premium_active() -> bool:
    """Whether any private premium overrides were loaded (useful for diagnostics)."""
    return bool(_OVERRIDES)


__all__ = [
    "COUNCIL_PLANNER",
    "COUNCIL_CODER",
    "COUNCIL_REVIEWER",
    "COUNCIL_CHALLENGER",
    "COUNCIL_SYNTHESIZER",
    "CHAT_INTERACTIVE",
    "CHAT_CONVERSATIONAL",
    "get_prompt",
    "is_premium_active",
]
