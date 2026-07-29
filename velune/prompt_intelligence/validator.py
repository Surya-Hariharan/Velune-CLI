"""Prompt validation: duplicate/conflict/overflow detection over the
assembled IR, before any renderer runs. See ``docs/02-prompt-intelligence.md``
§14 — failures are surfaced in a report and resolved per an explicit
precedence, never silently dropped.
"""

from __future__ import annotations

from velune.prompt_intelligence.ir import (
    PromptIR,
    ProviderProfile,
    ValidationIssue,
    ValidationReport,
)


class PromptValidator:
    """Runs over an assembled ``PromptIR`` before rendering."""

    def validate(self, ir: PromptIR, profile: ProviderProfile) -> ValidationReport:
        issues: list[ValidationIssue] = []
        issues.extend(self._check_duplicate_constraints(ir))
        issues.extend(self._check_untrusted_instruction_nodes(ir))
        issues.extend(self._check_token_budget(ir, profile))
        return ValidationReport(issues=issues)

    def _check_duplicate_constraints(self, ir: PromptIR) -> list[ValidationIssue]:
        counts: dict[str, int] = {}
        for c in ir.constraints:
            key = c.text.strip().lower()
            counts[key] = counts.get(key, 0) + 1

        return [
            ValidationIssue(
                code="duplicate_constraint",
                message=f"Constraint appears {count} times: {text!r}",
                severity="warning",
            )
            for text, count in counts.items()
            if count > 1
        ]

    def _check_untrusted_instruction_nodes(self, ir: PromptIR) -> list[ValidationIssue]:
        """§16 security boundary: a context node derived from a source that
        isn't fully trusted must never be marked instruction-bearing — that
        would let untrusted content pose as a system instruction merely
        because a renderer's delimiter style makes it look like one."""
        return [
            ValidationIssue(
                code="untrusted_instruction_node",
                message=(
                    "A context node with trust_score < 1.0 is marked "
                    "is_instruction_bearing=True — this must never happen "
                    "(docs/02-prompt-intelligence.md §16)."
                ),
                severity="error",
            )
            for node in ir.context_nodes
            if node.is_instruction_bearing and node.trust_score < 1.0
        ]

    def _check_token_budget(self, ir: PromptIR, profile: ProviderProfile) -> list[ValidationIssue]:
        if profile.max_system_tokens is None:
            return []

        approx_tokens = len(ir.role) // 4
        approx_tokens += sum(len(n.content) for n in ir.context_nodes) // 4
        if approx_tokens <= profile.max_system_tokens:
            return []

        return [
            ValidationIssue(
                code="system_prompt_over_budget",
                message=(
                    f"Approximate system-prompt size ({approx_tokens} tokens) exceeds "
                    f"{profile.family}'s {profile.max_system_tokens}-token ceiling; "
                    "the renderer will truncate."
                ),
                severity="warning",
            )
        ]
