"""OpenAI renderer.

No bespoke logic is needed beyond ``OPENAI_PROFILE`` (markdown headers,
role-task-context ordering, light-touch planning) — the shared algorithm in
``base.py`` already implements all of it. This class exists as a named
extension point per the class diagram in §12/§17, not because it currently
diverges from the shared implementation.
"""

from __future__ import annotations

from velune.prompt_intelligence.ir import PromptIR, ProviderProfile, RenderedPrompt
from velune.prompt_intelligence.renderers.base import ProviderRenderer, render_ir


class OpenAIRenderer(ProviderRenderer):
    def render(self, ir: PromptIR, profile: ProviderProfile) -> RenderedPrompt:
        return render_ir(ir, profile)
