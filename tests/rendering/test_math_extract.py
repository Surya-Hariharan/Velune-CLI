"""Math extraction: finding math in raw Markdown before Markdown parsing destroys it."""

from __future__ import annotations

from velune.cli.rendering.math.extract import MATH_FENCE, PENDING_FENCE, transform


def test_display_bracket_becomes_math_fence() -> None:
    out = transform(
        "Here:\n\n\\[ A = \\begin{bmatrix} 1 & 2 \\\\ 3 & 4 \\end{bmatrix} \\]\n\nDone."
    )
    assert f"```{MATH_FENCE}" in out
    # The TeX inside the fence is untouched — row separators survive.
    assert "1 & 2 \\\\ 3 & 4" in out


def test_double_dollar_display() -> None:
    out = transform("$$x^2 + y^2 = z^2$$")
    assert f"```{MATH_FENCE}" in out
    assert "x^2 + y^2 = z^2" in out


def test_bare_environment_pulls_in_lead() -> None:
    out = transform("A = \\begin{pmatrix} 1 & 0 \\\\ 0 & 1 \\end{pmatrix}")
    assert f"```{MATH_FENCE}\nA = \\begin{{pmatrix}}" in out


def test_environment_inside_inline_math_stays_inline() -> None:
    # Seen live from gpt-oss: a matrix inside \( \) must not swallow the prose.
    text = "* In the example \\(A=\\begin{pmatrix}4&2\\\\1&3\\end{pmatrix}\\) we found it."
    out = transform(text)
    assert out == "* In the example A = (4 2; 1 3) we found it."
    dollar = transform("so $v=\\begin{bmatrix}1\\\\2\\end{bmatrix}$ here")
    assert MATH_FENCE not in dollar and dollar.startswith("so v = ")


def test_inline_paren_and_dollar() -> None:
    assert transform("so \\(x^2\\) grows") == "so x² grows"
    assert transform("so $x_i$ is") == "so xᵢ is"


def test_currency_is_not_math() -> None:
    text = "It costs $5 and $10, or $ 20 $ maybe."
    assert transform(text) == text


def test_escaped_dollar_kept() -> None:
    assert transform("price \\$5 and \\$6") == "price \\$5 and \\$6"


def test_bare_symbol_in_prose() -> None:
    assert transform("a \\cdot b") == "a · b"


def test_windows_path_not_touched() -> None:
    text = "open C:\\alpha\\times.txt"
    assert transform(text) == text


def test_fenced_code_untouched() -> None:
    text = 'Code:\n\n```python\nequation = r"\\begin{bmatrix} $x$ \\[ \\]"\n```\n'
    assert transform(text) == text


def test_inline_code_untouched() -> None:
    text = "use `$x^2$` and `\\(y\\)` literally"
    assert transform(text) == text


def test_escaped_citation_not_math() -> None:
    assert MATH_FENCE not in transform("see \\[1\\] for details")


def test_table_cell_math_converted() -> None:
    text = "| a | b |\n|---|---|\n| \\(C_{11}\\) | $1\\cdot 2$ |"
    out = transform(text)
    assert "C₁₁" in out and "1 · 2" in out
    assert out.count("|") == text.count("|")


def test_unclosed_display_while_streaming_is_pending() -> None:
    out = transform("Result:\n\n\\[ A = \\begin{bmatrix} 1 & 2", streaming=True)
    assert f"```{PENDING_FENCE}" in out


def test_unclosed_display_final_shows_source() -> None:
    out = transform("Result: $$x^2 + ", streaming=False)
    assert PENDING_FENCE not in out and MATH_FENCE not in out
    assert "x^2" in out


def test_malformed_inline_keeps_source() -> None:
    out = transform("bad $\\frac{a}{$ here")
    assert "frac" in out


def test_list_item_indentation_kept() -> None:
    out = transform("1. First\n\n   $$x^2$$\n\n2. Second")
    assert f"   ```{MATH_FENCE}" in out


def test_crlf_normalised() -> None:
    assert "\r" not in transform("a $x^2$\r\nb")


def test_no_math_is_identity() -> None:
    text = "# Title\n\nJust **bold** text with a [link](http://x.y)."
    assert transform(text) is text
