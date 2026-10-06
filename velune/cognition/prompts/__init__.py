"""Central system-prompt library for Velune's council agents and chat surfaces.

Every LLM-facing system prompt in Velune is resolved through this package so the
wording lives in exactly one place and can be tuned without touching agent code.

Two layers exist:

* ``_baseline`` — committed, public-safe prompts. Always present, and the default
  everywhere, so the same installed version behaves identically on every machine.
* ``_premium`` — an **optional, git-ignored** module holding tuned "house" prompts.
  It is used **only when explicitly requested** with ``VELUNE_PROMPT_LAYER=premium``;
  merely having the file on disk changes nothing. When requested, its entries
  override the baseline key-for-key. If it was requested but is missing or unusable
  the baseline is used and a warning is logged — never silently.

Both layers expose a single ``PROMPTS: dict[str, str]`` mapping. Resolution is
``premium.get(key, baseline[key])`` for the premium layer and ``baseline[key]``
otherwise; a missing key in *both* raises loudly rather than silently sending an
empty system prompt to a model. See ``_premium.example.py`` for the contract.

``active_layer()`` reports which layer is live (and a short digest of the resolved
prompt text) so ``velune doctor`` and council traces can show it.

Prompt keys are namespaced ``"<surface>.<role>"`` (e.g. ``"council.planner"``,
``"chat.interactive"``). Use the module-level constants below rather than raw
strings so typos fail fast.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
from dataclasses import dataclass

from velune.cognition.prompts import _baseline, _deliberation

logger = logging.getLogger("velune.cognition.prompts")

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

PROMPT_LAYER_ENV = "VELUNE_PROMPT_LAYER"
_PREMIUM_MODULE = "velune.cognition.prompts._premium"

# (layer in use, overrides, layer requested) — resolved once, on first use.
_STATE: tuple[str, dict[str, str], str] | None = None


@dataclass(frozen=True)
class PromptLayerInfo:
    """Which prompt layer is live."""

    name: str  # "baseline" or "premium"
    requested: str  # what the environment asked for
    digest: str  # short hash of every resolved prompt, to tell layers apart
    premium_available: bool  # a _premium module exists on this machine


def _load_premium_overrides() -> dict[str, str]:
    """Import the optional, git-ignored premium prompt layer if it exists.

    Returns only well-formed ``str -> str`` entries; any failure yields ``{}``
    (the caller decides whether that deserves a warning).
    """
    try:
        from velune.cognition.prompts import _premium  # type: ignore[attr-defined]
    except Exception:
        return {}

    overrides = getattr(_premium, "PROMPTS", None)
    if not isinstance(overrides, dict):
        return {}
    return {
        k: v
        for k, v in overrides.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


def _resolve() -> tuple[str, dict[str, str], str]:
    global _STATE
    if _STATE is not None:
        return _STATE
    requested = (os.environ.get(PROMPT_LAYER_ENV) or "baseline").strip().lower()
    layer, overrides = "baseline", {}
    if requested == "premium":
        loaded = _load_premium_overrides()
        if loaded:
            layer, overrides = "premium", loaded
        else:
            logger.warning(
                "%s=premium but no usable premium prompts were found; using the baseline prompts.",
                PROMPT_LAYER_ENV,
            )
    elif requested != "baseline":
        logger.warning(
            "Unknown %s=%r (use 'baseline' or 'premium'); using the baseline prompts.",
            PROMPT_LAYER_ENV,
            requested,
        )
    _STATE = (layer, overrides, requested)
    return _STATE


def reset_prompt_layer() -> None:
    """Forget the resolved layer so the next lookup re-reads the environment (for tests)."""
    global _STATE
    _STATE = None


def get_prompt(key: str) -> str:
    """Resolve a system prompt by key from the active layer.

    Raises ``KeyError`` if the key is unknown in *both* layers — that is a programming
    error (a typo'd key), never something to paper over with an empty prompt.
    """
    _, overrides, _ = _resolve()
    if key in overrides:
        return overrides[key]
    try:
        return _baseline.PROMPTS[key]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(
            f"Unknown system-prompt key {key!r}. Known keys: {sorted(_baseline.PROMPTS)}"
        ) from exc


def get_deliberation_prompt(key: str) -> str:
    """Resolve a deliberative-council prompt (premium override, else the committed text).

    These live in ``_deliberation`` rather than ``_baseline`` so :func:`active_layer` and the
    legacy council prompts are unaffected. An unknown key raises, never an empty prompt.
    """
    _, overrides, _ = _resolve()
    if key in overrides:
        return overrides[key]
    try:
        return _deliberation.PROMPTS[key]
    except KeyError as exc:
        raise KeyError(
            f"Unknown deliberation prompt key {key!r}. Known keys: {sorted(_deliberation.PROMPTS)}"
        ) from exc


def deliberation_digest() -> str:
    """Short hash of the resolved deliberation prompts, for run records."""
    parts = [f"{key}|{get_deliberation_prompt(key)}" for key in sorted(_deliberation.PROMPTS)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def is_premium_active() -> bool:
    """Whether the premium layer was requested and loaded."""
    return _resolve()[0] == "premium"


def _premium_available() -> bool:
    """Whether a premium prompt module exists on this machine (without importing it)."""
    if sys.modules.get(_PREMIUM_MODULE) is not None:
        return True
    try:
        return importlib.util.find_spec(_PREMIUM_MODULE) is not None
    except (ImportError, ValueError):
        return False


def active_layer() -> PromptLayerInfo:
    """The live prompt layer, with a digest of the text it resolves to."""
    layer, _, requested = _resolve()
    digest = hashlib.sha256(
        "\n".join(f"{key}\0{get_prompt(key)}" for key in sorted(_baseline.PROMPTS)).encode("utf-8")
    ).hexdigest()[:12]
    return PromptLayerInfo(
        name=layer,
        requested=requested,
        digest=digest,
        premium_available=_premium_available(),
    )


__all__ = [
    "COUNCIL_PLANNER",
    "COUNCIL_CODER",
    "COUNCIL_REVIEWER",
    "COUNCIL_CHALLENGER",
    "COUNCIL_SYNTHESIZER",
    "CHAT_INTERACTIVE",
    "CHAT_CONVERSATIONAL",
    "PROMPT_LAYER_ENV",
    "PromptLayerInfo",
    "active_layer",
    "deliberation_digest",
    "get_deliberation_prompt",
    "get_prompt",
    "is_premium_active",
    "reset_prompt_layer",
]
