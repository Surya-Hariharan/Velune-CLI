"""SEARCH/REPLACE edit format parser with fuzzy block matching.

The LLM outputs change blocks in this shape:

    path/to/file.py
    <<<<<<< SEARCH
    exact lines to replace
    =======
    replacement lines
    >>>>>>> REPLACE

For new files the SEARCH section is empty.  For deletions the REPLACE section
is empty.  Multiple blocks may appear in a single response.

Fuzzy matching tolerates minor whitespace drift between what the model copied
and what is actually on disk, trying four strategies in order:
  1. Exact substring match
  2. Strip-normalised match (leading/trailing whitespace trimmed)
  3. Indent-normalised match (common leading indent removed per line)
  4. Sliding-window SequenceMatcher (ratio ≥ FUZZY_THRESHOLD)
"""

from __future__ import annotations

import re
import textwrap
from difflib import SequenceMatcher
from pathlib import Path

from velune.execution.edit_formats.base import BaseEditFormat, EditBlock, EditFormat, ParseError

FUZZY_THRESHOLD = 0.85

# Matches SEARCH/REPLACE fence pairs.  Tolerates ORIGINAL/MODIFIED aliases and
# optional backtick code-fence wrappers around the block.
_BLOCK_RE = re.compile(
    r"(?:^|\n)"
    r"(?:```[^\n]*\n)?"  # optional opening fence
    r"[ \t]*([^\n`<>=]+[^\s])"  # file path (trim surrounding space)
    r"[ \t]*\n"
    r"(?:```[^\n]*\n)?"  # optional fence after path
    r"<{7} (?:SEARCH|ORIGINAL)\n"  # SEARCH marker
    r"(.*?)"  # original content (may be empty)
    r"={7}\n"  # separator
    r"(.*?)"  # proposed content (may be empty)
    r">{7} (?:REPLACE|MODIFIED)",  # REPLACE marker
    re.DOTALL,
)

# Looser pattern for when the path appears above the entire fenced block.
_FENCE_BLOCK_RE = re.compile(
    r"(?:^|\n)([^\n`<>=]+\.[a-zA-Z0-9]+)[ \t]*\n"
    r"```[^\n]*\n"
    r"<{7} (?:SEARCH|ORIGINAL)\n"
    r"(.*?)"
    r"={7}\n"
    r"(.*?)"
    r">{7} (?:REPLACE|MODIFIED)\n"
    r"```",
    re.DOTALL,
)


def _normalize_indent(text: str) -> str:
    """Remove the common leading indent from every non-blank line."""
    return textwrap.dedent(text)


def _find_and_replace(content: str, search: str, replace: str) -> str | None:
    """Locate *search* inside *content* using four fuzzy strategies.

    Returns the patched content string or None if no match is found.
    """
    # 1. Exact
    if search in content:
        return content.replace(search, replace, 1)

    # 2. Strip-normalised
    s_strip = search.strip()
    if s_strip and s_strip in content:
        idx = content.find(s_strip)
        return content[:idx] + replace + content[idx + len(s_strip) :]

    # 3. Indent-normalised
    s_norm = _normalize_indent(search)
    if s_norm.strip() and s_norm in content:
        idx = content.find(s_norm)
        return content[:idx] + replace + content[idx + len(s_norm) :]

    # 4. Sliding-window SequenceMatcher
    search_lines = search.splitlines(keepends=True)
    content_lines = content.splitlines(keepends=True)
    n = len(search_lines)

    if n == 0 or len(content_lines) < n:
        return None

    best_ratio = 0.0
    best_start = -1

    for i in range(len(content_lines) - n + 1):
        window = "".join(content_lines[i : i + n])
        ratio = SequenceMatcher(None, search, window, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_ratio >= FUZZY_THRESHOLD and best_start >= 0:
        prefix = "".join(content_lines[:best_start])
        suffix = "".join(content_lines[best_start + n :])
        return prefix + replace + suffix

    return None


class SearchReplaceFormat(BaseEditFormat):
    """Parser for the <<<<<<< SEARCH / >>>>>>> REPLACE block format."""

    def parse(self, response: str, workspace_path: Path | None = None) -> list[EditBlock]:
        blocks: list[EditBlock] = []

        for pattern in (_BLOCK_RE, _FENCE_BLOCK_RE):
            for m in pattern.finditer(response):
                raw_path, original, proposed = m.group(1), m.group(2), m.group(3)
                file_path = raw_path.strip()

                # Skip obviously non-path matches (e.g. markdown headers)
                if not file_path or "\n" in file_path or len(file_path) > 260:
                    continue

                is_new = not original.strip()
                is_del = bool(original.strip()) and not proposed.strip()

                blocks.append(
                    EditBlock(
                        file_path=file_path,
                        original=original,
                        proposed=proposed,
                        is_new_file=is_new,
                        is_deletion=is_del,
                        format_used=EditFormat.SEARCH_REPLACE,
                    )
                )

        if not blocks:
            raise ParseError("No SEARCH/REPLACE blocks found in response")
        return blocks

    def apply_block(self, block: EditBlock, workspace_path: Path) -> str:
        """Apply a single EditBlock to the file on disk.

        Returns the new file content that should be written.
        Raises ParseError if the SEARCH block cannot be located.
        """
        target = workspace_path / block.file_path

        if block.is_new_file or not target.exists():
            return block.proposed

        if block.is_deletion:
            return ""

        current = target.read_text(encoding="utf-8", errors="replace")
        result = _find_and_replace(current, block.original, block.proposed)

        if result is None:
            raise ParseError(
                f"SEARCH block not found in {block.file_path} "
                f"(even with fuzzy matching at threshold {FUZZY_THRESHOLD})"
            )
        return result

    def format_instructions(self) -> str:
        return """\
### [EDIT FORMAT: SEARCH/REPLACE]
Output every code change as one or more SEARCH/REPLACE blocks. Use this exact structure:

path/to/file.py
<<<<<<< SEARCH
exact lines from the current file that will be replaced
=======
replacement lines (empty to delete the block)
>>>>>>> REPLACE

Rules:
- The SEARCH block MUST exactly match the current file content (whitespace included).
- Output multiple blocks to change multiple locations in the same file.
- For a NEW file: leave the SEARCH block empty (write nothing between the markers and =======).
- Always write the file path on the line immediately before <<<<<<< SEARCH.
- Never wrap blocks in additional markdown fences.
"""
