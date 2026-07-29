"""Gemini renderer.

The one genuinely bespoke behavior among the four renderers: with
``ContextOrdering.CONTEXT_FIRST``, ``render_ir`` places the assembled
context block ahead of the instruction block and inserts
``GEMINI_PROFILE.anchor_phrase`` between them — the "anchor" transition
sentence the Gemini research memo calls out, refocusing the model on the
task after a long context block. That behavior lives in ``base.py`` and is
already exercised by the profile; this class is the named seat for any
further Gemini-specific divergence.
"""

from __future__ import annotations

from velune.prompt_intelligence.ir import PromptIR, ProviderProfile, RenderedPrompt
from velune.prompt_intelligence.renderers.base import ProviderRenderer, render_ir


class GeminiRenderer(ProviderRenderer):
    def render(self, ir: PromptIR, profile: ProviderProfile) -> RenderedPrompt:
        return render_ir(ir, profile)
