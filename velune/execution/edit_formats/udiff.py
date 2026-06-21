"""Unified-diff edit format parser.

Parses standard git-style unified diffs produced by the LLM:

    --- a/path/to/file.py
    +++ b/path/to/file.py
    @@ -10,5 +10,6 @@
     context line
    -removed line
    +added line
     context line

Multiple files may appear in a single response, each introduced by a new
--- / +++ header pair.
"""

from __future__ import annotations

import re
from pathlib import Path

from velune.execution.edit_formats.base import BaseEditFormat, EditBlock, EditFormat, ParseError

_HEADER_RE = re.compile(
    r"^---[ \t]+(?:a/)?(.+?)\n"
    r"\+\+\+[ \t]+(?:b/)?(.+?)\n",
    re.MULTILINE,
)

_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n(.*?)(?=^@@|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _apply_hunks(content: str, hunks: list[re.Match]) -> str:
    """Apply a sequence of unified-diff hunks to *content*."""
    lines = content.splitlines(keepends=True)
    offset = 0

    for hunk in hunks:
        old_start = int(hunk.group(1)) - 1  # convert to 0-based
        old_count = int(hunk.group(2)) if hunk.group(2) is not None else 1
        hunk_lines = hunk.group(5).splitlines(keepends=True)

        removals: list[str] = []
        additions: list[str] = []

        for line in hunk_lines:
            if line.startswith("-"):
                removals.append(line[1:])
            elif line.startswith("+"):
                additions.append(line[1:])
            else:
                # Context line — belongs to both sides
                removals.append(line[1:] if line.startswith(" ") else line)
                additions.append(line[1:] if line.startswith(" ") else line)

        start = old_start + offset
        end = start + old_count
        lines[start:end] = additions
        offset += len(additions) - old_count

    return "".join(lines)


class UDiffFormat(BaseEditFormat):
    """Parser for standard unified-diff (--- / +++) output."""

    def parse(self, response: str, workspace_path: Path | None = None) -> list[EditBlock]:
        blocks: list[EditBlock] = []

        for header in _HEADER_RE.finditer(response):
            src_path = header.group(1).strip()
            dst_path = header.group(2).strip()

            # Use the dst path (after the change) as the canonical path
            file_path = dst_path if dst_path != "/dev/null" else src_path

            # Extract hunks that follow this header
            diff_start = header.end()
            next_header = _HEADER_RE.search(response, diff_start)
            diff_end = next_header.start() if next_header else len(response)
            diff_section = response[diff_start:diff_end]

            hunks = list(_HUNK_RE.finditer(diff_section))

            is_new = src_path == "/dev/null"
            is_del = dst_path == "/dev/null"

            original = ""
            proposed = ""

            if workspace_path and not is_new:
                target = workspace_path / file_path
                if target.exists():
                    original = target.read_text(encoding="utf-8", errors="replace")
                    if not is_del:
                        try:
                            proposed = _apply_hunks(original, hunks)
                        except Exception:
                            proposed = original

            blocks.append(
                EditBlock(
                    file_path=file_path,
                    original=original,
                    proposed=proposed,
                    is_new_file=is_new,
                    is_deletion=is_del,
                    format_used=EditFormat.UDIFF,
                    confidence=0.9,
                )
            )

        if not blocks:
            raise ParseError("No unified-diff blocks found in response")
        return blocks

    def format_instructions(self) -> str:
        return """\
### [EDIT FORMAT: UNIFIED DIFF]
Output changes as standard unified diffs:

--- a/path/to/file.py
+++ b/path/to/file.py
@@ -10,5 +10,6 @@
 context line
-removed line
+added line
 context line

Rules:
- Include 3 lines of context around each changed section.
- Use /dev/null as the source for new files, target for deletions.
- One --- / +++ header pair per file.
"""
