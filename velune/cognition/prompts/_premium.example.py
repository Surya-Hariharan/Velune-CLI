"""Template for the private premium prompt layer.

Copy this file to ``_premium.py`` (same directory) and fill in your tuned "house"
prompts. ``_premium.py`` is git-ignored on purpose, so its contents never enter
version control; this example IS committed so the contract stays discoverable.

Rules:
* Export ``PROMPTS: dict[str, str]`` keyed exactly like the baseline layer
  (see ``_baseline.py`` for the full key list and the JSON output contracts that
  the planner / reviewer / challenger prompts MUST preserve).
* Any key you omit transparently falls back to the committed baseline, so you can
  override just the prompts you want to tune.
* Empty or non-string values are ignored, so a half-finished entry can't ship a
  blank system prompt to a model.
"""

from __future__ import annotations

PROMPTS: dict[str, str] = {
    # "council.planner": "...your tuned planner prompt (keep the JSON contract)...",
    # "council.coder": "...",
    # "council.reviewer": "...keep the JSON contract...",
    # "council.challenger": "...keep the JSON contract...",
    # "council.synthesizer": "...",
    # "chat.interactive": "...",
    # "chat.conversational": "...",
}
