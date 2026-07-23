"""Hunk-level interactive review of file diffs.

Splits a FileDiff into individual @@ hunks and lets the user accept or reject
each one independently, producing a merged output file.
"""

from __future__ import annotations

import asyncio
import difflib
from dataclasses import dataclass, field
from enum import Enum

from velune.execution.diff_preview import FileDiff, diff_stats, format_stat_bar


class HunkDecision(Enum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass
class HunkResult:
    hunk_index: int
    decision: HunkDecision
    # Opcode groups that produced this hunk (for reconstruction)
    opcodes: list[tuple] = field(default_factory=list)


class HunkReviewer:
    """Splits a FileDiff into individual hunks and prompts the user per hunk."""

    CONTEXT_LINES = 3

    def __init__(self, console) -> None:
        self.console = console

    def split_into_hunks(self, diff: FileDiff) -> list[list[tuple]]:
        """Return a list of opcode groups, one per hunk.

        Uses SequenceMatcher.get_grouped_opcodes() with CONTEXT_LINES context.
        Each element is a list of (tag, i1, i2, j1, j2) tuples.
        """
        original_lines = diff.original.splitlines(keepends=True)
        proposed_lines = diff.proposed.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, original_lines, proposed_lines, autojunk=False)
        return list(matcher.get_grouped_opcodes(self.CONTEXT_LINES))

    def render_hunk(
        self,
        diff: FileDiff,
        opcode_group: list[tuple],
        hunk_index: int,
        total: int,
    ) -> None:
        """Print one hunk as a Rich Panel."""
        from rich.panel import Panel
        from rich.syntax import Syntax

        original_lines = diff.original.splitlines(keepends=True)
        proposed_lines = diff.proposed.splitlines(keepends=True)

        hunk_lines: list[str] = []
        for tag, i1, i2, j1, j2 in opcode_group:
            if tag == "equal":
                for line in original_lines[i1:i2]:
                    hunk_lines.append(" " + line.rstrip("\n"))
            elif tag in ("replace", "delete"):
                for line in original_lines[i1:i2]:
                    hunk_lines.append("-" + line.rstrip("\n"))
            if tag in ("replace", "insert"):
                for line in proposed_lines[j1:j2]:
                    hunk_lines.append("+" + line.rstrip("\n"))

        diff_text = "\n".join(hunk_lines)
        self.console.print(
            Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=False),
                title=f"[yellow]Hunk {hunk_index + 1}/{total}[/yellow]  [dim]{diff.path.name}[/dim]",
                border_style="yellow",
                padding=(0, 1),
            )
        )

    async def review_hunks(
        self,
        diff: FileDiff,
        auto_accept: bool = False,
    ) -> str:
        """Interactively review each hunk. Returns the merged proposed content.

        Accepted hunks keep the proposed change; rejected hunks revert to original.
        """
        if auto_accept:
            return diff.proposed

        hunk_groups = self.split_into_hunks(diff)
        if not hunk_groups:
            return diff.proposed

        # A file split into many hunks is exactly the case where a reviewer
        # benefits from knowing the overall scale before clicking through
        # them one at a time — same rationale as DiffPreview's stat bar for
        # a large unified diff.
        if len(hunk_groups) > 5:
            added, removed = diff_stats(diff)
            self.console.print(
                format_stat_bar(added, removed, label=f"{len(hunk_groups)} hunks to review")
            )

        results: list[HunkResult] = []
        for idx, group in enumerate(hunk_groups):
            self.render_hunk(diff, group, idx, len(hunk_groups))
            decision = await self._prompt_hunk_decision()
            results.append(HunkResult(hunk_index=idx, decision=decision, opcodes=group))

        return self._reconstruct(diff, hunk_groups, results)

    async def _prompt_hunk_decision(self) -> HunkDecision:
        from rich.prompt import Prompt

        action = await asyncio.to_thread(
            Prompt.ask,
            "\n  [dim][a]ccept / [r]eject[/dim]",
            choices=["a", "r", "accept", "reject"],
            default="a",
        )
        return HunkDecision.ACCEPT if action.lower() in ("a", "accept") else HunkDecision.REJECT

    def _reconstruct(
        self,
        diff: FileDiff,
        hunk_groups: list[list[tuple]],
        results: list[HunkResult],
    ) -> str:
        """Apply per-hunk decisions to produce the final file content.

        For each position in the original file:
        - If covered by an ACCEPT hunk → use the proposed lines for that region.
        - If covered by a REJECT hunk → keep the original lines.
        - If not covered by any hunk → keep original (context/unchanged area).
        """
        original_lines = diff.original.splitlines(keepends=True)
        proposed_lines = diff.proposed.splitlines(keepends=True)

        # Build a mapping: original line index → (accept, proposed_slice)
        # We process the full file region by region.
        out: list[str] = []
        orig_pos = 0

        for result in results:
            for tag, i1, i2, j1, j2 in result.opcodes:
                # Copy any original lines before this opcode that weren't covered
                if i1 > orig_pos:
                    out.extend(original_lines[orig_pos:i1])
                    orig_pos = i1

                if tag == "equal":
                    out.extend(original_lines[i1:i2])
                    orig_pos = i2
                elif result.decision == HunkDecision.ACCEPT:
                    # Apply the change: skip original, use proposed
                    if tag in ("replace", "insert"):
                        out.extend(proposed_lines[j1:j2])
                    orig_pos = i2
                else:
                    # Reject: keep original, skip proposed
                    if tag in ("replace", "delete"):
                        out.extend(original_lines[i1:i2])
                    orig_pos = i2

        # Append any remaining original lines after the last hunk
        if orig_pos < len(original_lines):
            out.extend(original_lines[orig_pos:])

        return "".join(out)
