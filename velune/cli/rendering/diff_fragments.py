"""Render a FileDiff as prompt_toolkit fragment lines for the transcript pane.

Pure fragment construction — no Rich round-trip — so the diff block can be
appended directly via ``FullscreenREPLUI.append_fragment_lines`` and stays
cheap to re-render (fragments are parsed once, at append time).

Style classes (defined in ``velune.cli.fullscreen``): ``diff.add``,
``diff.del``, ``diff.hunk``, ``diff.meta``, plus ``conversation.system``
for unchanged context lines.
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.execution.diff_preview import FileDiff

FragmentLine = list[tuple[str, str]]

_STYLE_ADD = "class:diff.add"
_STYLE_DEL = "class:diff.del"
_STYLE_HUNK = "class:diff.hunk"
_STYLE_META = "class:diff.meta"
_STYLE_CTX = "class:conversation.system"


def _clamp(text: str, width: int) -> str:
    if width > 1 and len(text) > width:
        return text[: width - 1] + "…"
    return text


def render_diff_fragments(
    diff: FileDiff,
    width: int,
    max_lines: int = 30,
    indent: str = "    ",
) -> list[FragmentLine]:
    """Fragment lines for one file change, truncated to ``max_lines``."""
    body_width = max(8, width - len(indent))
    out: list[FragmentLine] = []

    if diff.is_deletion and not diff.is_new_file:
        removed = len(diff.original.splitlines())
        out.append(
            [(_STYLE_DEL, _clamp(f"{indent}- {diff.path} (deleted, {removed} lines)", width))]
        )
        return out

    if diff.is_new_file:
        lines = diff.proposed.splitlines()
        for raw in lines[:max_lines]:
            out.append([(_STYLE_ADD, indent + _clamp("+ " + raw, body_width))])
        if len(lines) > max_lines:
            out.append([(_STYLE_META, f"{indent}… +{len(lines) - max_lines} more lines")])
        return out

    udiff = list(
        difflib.unified_diff(
            diff.original.splitlines(),
            diff.proposed.splitlines(),
            lineterm="",
        )
    )
    # Skip the ---/+++ file headers; the tool card above already names the file.
    body = [ln for ln in udiff if not ln.startswith(("---", "+++"))]
    if not body:
        return [[(_STYLE_META, f"{indent}(no changes)")]]

    for raw in body[:max_lines]:
        if raw.startswith("@@"):
            style = _STYLE_HUNK
        elif raw.startswith("+"):
            style = _STYLE_ADD
        elif raw.startswith("-"):
            style = _STYLE_DEL
        else:
            style = _STYLE_CTX
        out.append([(style, indent + _clamp(raw, body_width))])
    if len(body) > max_lines:
        out.append([(_STYLE_META, f"{indent}… +{len(body) - max_lines} more lines")])
    return out
