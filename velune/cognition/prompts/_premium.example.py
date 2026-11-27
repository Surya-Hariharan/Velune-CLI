"""Contract for the optional, git-ignored premium prompt layer.

Copy this file to ``_premium.py`` (same folder) and fill ``PROMPTS``. It is used only
when ``VELUNE_PROMPT_LAYER=premium`` is set; otherwise the committed ``_baseline.py``
prompts apply, so a stray ``_premium.py`` never changes behaviour on its own.

Export ``PROMPTS: dict[str, str]`` keyed by the same namespaced keys as
``_baseline.PROMPTS`` (``"chat.interactive"``, ``"chat.conversational"``). Keys you
omit fall back to the baseline text; unknown keys are ignored.
"""

from __future__ import annotations

PROMPTS: dict[str, str] = {}
