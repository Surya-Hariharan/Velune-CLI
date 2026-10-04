"""Deduplication + adaptive truncation + provider-specific budget fit.

Delegates numeric budget allocation to ``ContextBudget``/``ContextAssembler``
(unchanged); this module only adds the per-family system-prompt ceiling on
top. See the Prompt Intelligence design notes §6, §15.
"""

from __future__ import annotations

from velune.prompt_intelligence.ir import PromptIR, ProviderProfile


class TokenOptimizer:
    """Cross-section dedup + per-family budget fit over an assembled IR."""

    def dedupe(self, ir: PromptIR) -> PromptIR:
        """Drop constraint duplicates, keeping first occurrence order."""
        seen: set[str] = set()
        deduped = []
        for c in ir.constraints:
            key = c.text.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
        ir.constraints = deduped
        return ir

    def fit_to_budget(self, ir: PromptIR, profile: ProviderProfile) -> PromptIR:
        """Drop lowest-trust context nodes first until under *profile*'s ceiling.

        Role/constraints/workflow are never dropped here — only the
        (already most-droppable, per ContextAssembler's own policy) context
        nodes are candidates; the renderer's own truncation is the last
        resort if even that isn't enough (§11).
        """
        if profile.max_system_tokens is None or not ir.context_nodes:
            return ir

        budget_chars = profile.max_system_tokens * 4
        kept = []
        used = 0
        for node in sorted(ir.context_nodes, key=lambda n: n.trust_score, reverse=True):
            if used + len(node.content) <= budget_chars:
                kept.append(node)
                used += len(node.content)
        ir.context_nodes = kept
        return ir
