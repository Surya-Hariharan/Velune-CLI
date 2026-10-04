"""Tables, mermaid flowcharts and ASCII charts render legibly in the terminal."""

from __future__ import annotations

from rich.console import Console

from velune.cli.rendering.markdown import CustomMarkdown
from velune.cli.rendering.mermaid import render_mermaid
from velune.cli.rendering.segments_to_pt import render_to_fragments


def _render(md: str, width: int) -> str:
    frags = render_to_fragments(Console(), CustomMarkdown(md), width)
    return "\n".join("".join(text for _style, text in line) for line in frags)


def test_table_has_borders_and_wraps_instead_of_truncating():
    md = (
        "| Provider | Description |\n| --- | --- |\n"
        "| groq | Large open-weight reasoning model with long context |\n"
    )
    out = _render(md, 50)
    # Rounded corners on most terminals; Windows' legacy console falls back to square ones.
    assert ("╭" in out or "┌" in out) and "│" in out and ("╰" in out or "└" in out)
    assert "…" not in out
    for word in ("Large", "open-weight", "reasoning", "context"):
        assert word in out


def test_chart_block_is_cropped_not_wrapped():
    md = "```\nLatency  " + "█" * 60 + " 80ms trailing\nCost     ███\n```\n"
    lines = [ln for ln in _render(md, 30).splitlines() if "Latency" in ln or "Cost" in ln]
    assert len(lines) == 2  # one rendered line per source line: nothing wrapped onto a 3rd
    assert all(len(ln.rstrip()) <= 30 for ln in lines)


def test_mermaid_flowchart_renders_every_label():
    out = render_mermaid("graph TD\n  A[Start] --> B[Mid]\n  A --> C[End]", 80)
    assert out is not None
    text = out.plain
    for label in ("Start", "Mid", "End"):
        assert label in text
    assert "├─▶" in text and "└─▶" in text


def test_mermaid_edge_labels_are_kept():
    out = render_mermaid("flowchart LR\n  A -->|yes| B", 80)
    assert out is not None and "yes" in out.plain


def test_mermaid_cycle_terminates():
    out = render_mermaid("graph TD\n  A --> B\n  B --> A", 80)
    assert out is not None and "↩" in out.plain


def test_unsupported_mermaid_falls_back_to_code_block():
    assert render_mermaid('pie title Pets\n  "Dogs" : 386', 80) is None
    out = _render('```mermaid\npie title Pets\n  "Dogs" : 386\n```\n', 60)
    assert "pie title Pets" in out


def test_mermaid_subroutine_and_round_shapes_drop_extra_brackets():
    out = render_mermaid("graph TD" + chr(10) + "  A[[Client]] --> B((Server))", 80)
    assert out is not None
    assert "[Client]" in out.plain and "[[Client]]" not in out.plain
    assert "[Server]" in out.plain
