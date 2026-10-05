"""The TeX-subset math engine: parser, inline (linear) form and 2D display layout."""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from velune.cli.rendering.math import render_display, render_inline
from velune.cli.rendering.math.tex import MathParseError, Matrix, parse

# ── Parser ───────────────────────────────────────────────────────────────────


def test_parse_matrix_rows_and_cells() -> None:
    row = parse(r"\begin{bmatrix} 1 & 2 & 3 \\ 4 & 5 & 6 \end{bmatrix}")
    matrices = [n for n in row.items if isinstance(n, Matrix)]
    assert len(matrices) == 1
    assert matrices[0].env == "bmatrix"
    assert len(matrices[0].rows) == 2
    assert all(len(r) == 3 for r in matrices[0].rows)


@pytest.mark.parametrize("src", [r"\frac{a}{", r"x^{2", r"\begin{bmatrix} 1 & 2"])
def test_parse_structural_errors_raise(src: str) -> None:
    with pytest.raises(MathParseError):
        parse(src)


def test_unknown_command_is_kept_not_fatal() -> None:
    assert render_inline(r"\foo + 1") is not None


# ── Inline (one line) ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tex", "expected"),
    [
        (r"x^2", "x²"),
        (r"x_i", "xᵢ"),
        (r"C_{ij}", "Cᵢⱼ"),
        (r"C_{11}", "C₁₁"),
        (r"e^{-x}", "e⁻ˣ"),
        (r"A = \pi r^2", "A = πr²"),
        (r"(m \times n)", "(m × n)"),
        (r"1\cdot(-1)+2\cdot3+3\cdot0", "1 · (−1) + 2 · 3 + 3 · 0"),
        (r"\alpha \le \beta", "α ≤ β"),
        (r"\mathbb{R}^n", "ℝⁿ"),
        (r"\binom{n}{k}", "C(n, k)"),
        (r"f: A \to B", "f : A → B"),
        (r"x := 2", "x := 2"),
        (r"a \Longrightarrow b", "a ⟹ b"),
    ],
)
def test_inline_unicode(tex: str, expected: str) -> None:
    assert render_inline(tex) == expected


def test_unbraced_single_token_arguments() -> None:
    # Seen live from gpt-oss: TeX allows a one-token argument without braces.
    # × has no superscript glyph, so the exponent falls back to ^( ).
    assert render_inline(r"A\in\mathbb R^{m\times n}") == "A ∈ ℝ^(m×n)"
    assert render_inline(r"\frac12") == "1/2"


@pytest.mark.parametrize(
    ("tex", "expected"),
    [
        (r"\boxed{C = AB}", "C = AB"),
        (r"x = 1 \tag{3}", "x = 1  (3)"),
        (r"x \label{eq:x}", "x"),
    ],
)
def test_structural_commands_seen_live(tex: str, expected: str) -> None:
    assert render_inline(tex) == expected


def test_array_with_hline_has_no_command_text() -> None:
    lines = render_display(r"\begin{array}{c|c} \hline a & b \\ \hline c & d \\ \hline \end{array}")
    assert lines is not None
    assert "hline" not in "\n".join(lines)


def test_phantom_is_invisible() -> None:
    lines = render_display(r"\begin{bmatrix} -1 & 0 \\ \phantom{-}3 & 2 \end{bmatrix}")
    assert lines is not None
    assert "phantom" not in "\n".join(lines)
    assert lines[1].lstrip("⎣ ").startswith("3")


def test_inline_ascii_has_no_non_ascii() -> None:
    for tex in (r"x^2 + \alpha", r"\sum_{k=1}^{n} k", r"\sqrt{2} \cdot \pi", r"a \le b"):
        out = render_inline(tex, ascii_only=True)
        assert out is not None
        assert out.isascii(), out


def test_inline_malformed_returns_none() -> None:
    assert render_inline(r"\frac{a}{") is None


# ── Display (2D) ─────────────────────────────────────────────────────────────


def test_bmatrix_3x3_layout() -> None:
    lines = render_display(r"A=\begin{bmatrix} 1 & 2 & 3\\ 0 & 1 & 4 \\ 5 & 6 & 0 \end{bmatrix}")
    assert lines == [
        "    ⎡ 1  2  3 ⎤",
        "A = ⎢ 0  1  4 ⎥",
        "    ⎣ 5  6  0 ⎦",
    ]


def test_sum_with_limits() -> None:
    lines = render_display(r"C_{ij} = \sum_{k=1}^{n} A_{ik}B_{kj}")
    assert lines is not None
    assert len(lines) == 3
    assert "Σ" in lines[1] and "Cᵢⱼ" in lines[1]
    assert lines[0].strip() == "n"
    assert lines[2].strip() == "k=1"


def test_fraction_is_stacked() -> None:
    lines = render_display(r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}")
    assert lines is not None
    # Numerator (radical overbar + body), fraction rule on the baseline, denominator.
    rule = next(line for line in lines if line.startswith("x ="))
    assert "─" in rule
    assert lines.index(rule) == len(lines) - 2
    assert lines[-1].strip() == "2a"
    assert "√" in "\n".join(lines)


@pytest.mark.parametrize(
    ("env", "left", "right"),
    [("pmatrix", "⎛", "⎞"), ("bmatrix", "⎡", "⎤"), ("vmatrix", "│", "│"), ("Vmatrix", "‖", "‖")],
)
def test_matrix_delimiters(env: str, left: str, right: str) -> None:
    lines = render_display(rf"\begin{{{env}}} a & b \\ c & d \end{{{env}}}")
    assert lines is not None
    assert lines[0].lstrip().startswith(left)
    assert lines[0].rstrip().endswith(right)


def test_rectangular_and_4x4_matrices() -> None:
    rect = render_display(r"\begin{bmatrix} 1 & 2 & 3 & 4 \\ 5 & 6 & 7 & 8 \end{bmatrix}")
    assert rect is not None and len(rect) == 2
    big = render_display(r"\begin{pmatrix} 1&0&0&0\\0&1&0&0\\0&0&1&0\\0&0&0&1 \end{pmatrix}")
    assert big is not None and len(big) == 4


def test_matrix_cells_with_negatives_decimals_fractions() -> None:
    lines = render_display(r"\begin{pmatrix} 1.5 & -2 \\ \frac{1}{2} & 10 \end{pmatrix}")
    assert lines is not None
    joined = "\n".join(lines)
    assert "1.5" in joined and "−2" in joined and "10" in joined
    # Every row has the same display width: columns are aligned.
    assert len({cell_len(line) for line in lines}) == 1


def test_cases_brace() -> None:
    lines = render_display(
        r"f(x) = \begin{cases} x^2 & x \ge 0 \\ -x & \text{otherwise} \end{cases}"
    )
    assert lines is not None
    assert "otherwise" in "\n".join(lines)
    assert any(ch in "\n".join(lines) for ch in "⎧⎨⎩{")


def test_too_wide_falls_back_to_one_line() -> None:
    lines = render_display(r"\begin{bmatrix} 1 & 2 & 3 & 4 \\ 5 & 6 & 7 & 8 \end{bmatrix}", 12)
    assert lines is not None and len(lines) == 1


def test_display_ascii_only() -> None:
    lines = render_display(
        r"A=\begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix} + \frac{a}{b}", ascii_only=True
    )
    assert lines is not None
    assert all(line.isascii() for line in lines), lines


def test_display_malformed_returns_none() -> None:
    assert render_display(r"\frac{a}{") is None


def test_hostile_input_never_raises() -> None:
    for tex in (
        "{" * 500,
        "}" * 50,
        r"\left(",
        r"\right)",
        r"\begin{nope} x \end{nope}",
        "^_^_",
        "",
    ):
        render_inline(tex)
        render_display(tex)
