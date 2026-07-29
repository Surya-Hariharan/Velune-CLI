"""Universal fallback renderer — used for any family without a bespoke
renderer, and for any family without even a registered profile (which then
also gets ``GENERIC_PROFILE`` — see profiles/__init__.py). See §11, §14.
"""

from __future__ import annotations

from velune.prompt_intelligence.ir import PromptIR, ProviderProfile, RenderedPrompt
from velune.prompt_intelligence.renderers.base import ProviderRenderer, render_ir


class GenericRenderer(ProviderRenderer):
    def render(self, ir: PromptIR, profile: ProviderProfile) -> RenderedPrompt:
        return render_ir(ir, profile)
