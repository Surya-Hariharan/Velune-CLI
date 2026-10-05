"""Streaming: every partial render succeeds, and the final render equals the one-shot render."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.rendering.test_render_golden import render
from velune.cli.rendering.markdown import CustomMarkdown, MarkdownStreamBuffer

FIXTURES = Path(__file__).parent / "fixtures"


def _render_renderable(renderable: CustomMarkdown, width: int = 80) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=width, color_system=None, force_terminal=False).print(renderable)
    return buf.getvalue()


def _hostile_cuts(text: str) -> list[int]:
    """Split points inside the constructs most likely to flicker."""
    cuts = set(range(0, len(text), 7))
    for needle in ("\\begin{bmat", "**the ans", "$x^", "\\[", "$$\\sum", "\\frac{n(n", "```py"):
        k = text.find(needle)
        if k != -1:
            cuts.update({k, k + 1, k + len(needle) // 2, k + len(needle)})
    return sorted(c for c in cuts if 0 < c < len(text))


@pytest.mark.parametrize("name", ["streaming_math", "screenshot_matrix_product", "mixed_content"])
def test_every_partial_render_succeeds_and_final_matches(name: str) -> None:
    text = (FIXTURES / f"{name}.md").read_text(encoding="utf-8")
    buffer = MarkdownStreamBuffer()
    last = 0
    for cut in [*_hostile_cuts(text), len(text)]:
        buffer.append(text[last:cut])
        last = cut
        partial = _render_renderable(buffer.get_renderable())
        # No raw environment source flashes while a formula is half-received.
        assert "\\begin{bmatrix}" not in partial.split("import numpy")[0]
    final = _render_renderable(buffer.get_renderable(final=True))
    one_shot = _render_renderable(CustomMarkdown(text))
    assert final == one_shot


def test_unclosed_formula_renders_progressively() -> None:
    buffer = MarkdownStreamBuffer()
    buffer.append("Result:\n\n\\[ A = \\begin{bmatrix} 1 & 2 \\\\ 3")
    out = _render_renderable(buffer.get_renderable())
    # The rows that have arrived are laid out; no raw TeX on screen.
    assert "⎡ 1  2 ⎤" in out
    assert "\\[" not in out and "\\begin" not in out


def test_unrenderable_partial_shows_placeholder() -> None:
    buffer = MarkdownStreamBuffer()
    buffer.append("Result: $$\\frac{n(n")
    out = _render_renderable(buffer.get_renderable())
    assert "…" in out and "\\frac" not in out


@pytest.mark.parametrize(
    ("partial", "closed"),
    [
        ("A = \\begin{bmat", "A ="),
        ("\\begin{bmatrix} 1 & 2 \\\\ 3", "\\begin{bmatrix} 1 & 2 \\\\ 3\\end{bmatrix}"),
        ("\\begin{bmatrix} 1 & ", "\\begin{bmatrix} 1\\end{bmatrix}"),
        ("\\left( x", "\\left( x\\right."),
        ("x^{2", "x^{2}"),
        ("\\{ a", "\\{ a"),
    ],
)
def test_close_partial(partial: str, closed: str) -> None:
    from velune.cli.rendering.math import close_partial

    assert close_partial(partial) == closed


def test_fullscreen_stream_render_passes_streaming_flag() -> None:
    from velune.cli import fullscreen

    source = Path(fullscreen.__file__).read_text(encoding="utf-8")
    assert "CustomMarkdown(stabilized, streaming=not final)" in source


def test_rerender_cost_is_bounded() -> None:
    """A long answer with many equations re-renders fast enough for a 0.08 s cadence."""
    block = (FIXTURES / "screenshot_matrix_product.md").read_text(encoding="utf-8")
    text = "\n\n".join([block] * 5)  # ~20 display formulas, ~150 lines
    render(text, 100)  # warm the layout caches
    start = time.perf_counter()
    render(text, 100)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"re-render took {elapsed:.3f}s"
