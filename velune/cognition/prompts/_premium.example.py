"""Contract for the optional, git-ignored premium prompt layer.

Copy this file to ``_premium.py`` (same folder) and fill ``PROMPTS``. It is used only
when ``VELUNE_PROMPT_LAYER=premium`` is set; otherwise the committed ``_baseline.py``
prompts apply, so a stray ``_premium.py`` never changes behaviour on its own.

Rules the premium layer must keep:

* Export ``PROMPTS: dict[str, str]`` keyed by the same namespaced keys as
  ``_baseline.PROMPTS`` (``"council.planner"``, ``"chat.interactive"``, ...). Keys you
  omit fall back to the baseline text; unknown keys are ignored.
* The planner, reviewer and challenger prompts emit JSON that is parsed into typed
  models (see ``velune/cognition/council/messages.py``). Keep the exact field names
  and shapes of the ``JSON Format:`` block in the baseline prompt; enrich the guidance,
  never the field names. ``tests/test_prompt_layer.py`` checks this when the file exists.
"""

from __future__ import annotations

PROMPTS: dict[str, str] = {}
