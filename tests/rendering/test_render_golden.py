"""Golden snapshots of whole responses rendered through CustomMarkdown.

Regenerate after an intentional rendering change with::

    VELUNE_UPDATE_GOLDEN=1 pytest tests/rendering/test_render_golden.py
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from velune.cli.rendering.markdown import CustomMarkdown

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
NAMES = sorted(p.stem for p in FIXTURES.glob("*.md"))
UPDATE = os.environ.get("VELUNE_UPDATE_GOLDEN") == "1"


class _Cp1252File(io.StringIO):
    """A file whose encoding can't show Unicode math: Rich sets ascii_only."""

    encoding = "cp1252"  # type: ignore[assignment]


def render(markup: str, width: int, *, ascii_only: bool = False) -> str:
    from rich.console import Console

    buf: io.StringIO = _Cp1252File() if ascii_only else io.StringIO()
    console = Console(
        file=buf, width=width, color_system=None, force_terminal=False, legacy_windows=False
    )
    console.print(CustomMarkdown(markup))
    return "\n".join(line.rstrip() for line in buf.getvalue().splitlines()) + "\n"


def _variants() -> list[tuple[str, int, bool]]:
    return [(n, w, False) for n in NAMES for w in (80, 120)] + [(n, 80, True) for n in NAMES]


@pytest.mark.parametrize(("name", "width", "ascii_only"), _variants())
def test_golden(name: str, width: int, ascii_only: bool) -> None:
    source = (FIXTURES / f"{name}.md").read_text(encoding="utf-8")
    actual = render(source, width, ascii_only=ascii_only)
    suffix = "ascii" if ascii_only else f"w{width}"
    golden = GOLDEN / f"{name}.{suffix}.txt"
    if UPDATE or not golden.exists():
        GOLDEN.mkdir(exist_ok=True)
        golden.write_text(actual, encoding="utf-8", newline="\n")
        if not UPDATE:
            pytest.fail(f"golden {golden.name} was missing; written — review and re-run")
    assert actual == golden.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", NAMES)
def test_no_raw_tex_leaks(name: str) -> None:
    if name == "malformed_math":
        pytest.skip("malformed input is shown as source by design")
    out = render((FIXTURES / f"{name}.md").read_text(encoding="utf-8"), 100)
    code_free = out.split("import numpy")[0]  # code blocks may hold TeX on purpose
    for token in ("\\begin", "\\end{", "\\frac", "\\cdot", "\\times", "\\[", "\\("):
        assert token not in code_free, f"{token!r} leaked into {name}"


@pytest.mark.parametrize("name", NAMES)
def test_ascii_render_is_ascii_outside_markdown_chrome(name: str) -> None:
    out = render((FIXTURES / f"{name}.md").read_text(encoding="utf-8"), 80, ascii_only=True)
    math_glyphs = set("⎡⎢⎣⎤⎥⎦⎛⎜⎝⎞⎟⎠⎧⎨⎩─√Σ∫·×≤≥±πα²ᵢⱼ₁")
    assert not (set(out) & math_glyphs), sorted(set(out) & math_glyphs)


@pytest.mark.parametrize("width", [20, 40, 60, 160])
def test_any_width_renders(width: int) -> None:
    for name in NAMES:
        out = render((FIXTURES / f"{name}.md").read_text(encoding="utf-8"), width)
        assert out.strip()


def test_redirected_output_has_no_escape_codes() -> None:
    out = render((FIXTURES / "screenshot_matrix_product.md").read_text(encoding="utf-8"), 100)
    assert "\x1b" not in out


def test_kill_switch_restores_previous_behaviour() -> None:
    from velune.cli.rendering import math as velune_math

    velune_math.set_enabled(False)
    try:
        out = render("$x^2$", 80)
    finally:
        velune_math.set_enabled(True)
    assert "x^2" in out
