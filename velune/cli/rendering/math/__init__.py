"""Terminal rendering of LaTeX-style math in assistant responses.

Models write standard Markdown + LaTeX (``$x^2$``, ``\\[ … \\]``,
``\\begin{bmatrix} … \\end{bmatrix}``); this package presents it in a terminal:
display math as a 2D Unicode layout (``layout``), inline math as one line
(``linear``), with an ASCII glyph set for consoles that can't show Unicode.

Rendering never raises: a formula that can't be parsed is shown as its
readable source. ``extract`` finds the math in a Markdown document (never
inside code) before Markdown's backslash escapes can destroy it.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

from velune.cli.rendering.math import layout as _layout
from velune.cli.rendering.math import linear as _linear
from velune.cli.rendering.math.tex import MathParseError, parse

logger = logging.getLogger("velune.cli.rendering.math")

__all__ = [
    "MathParseError",
    "close_partial",
    "render_display",
    "render_inline",
    "set_enabled",
    "enabled",
]

_ENABLED = True


def set_enabled(value: bool) -> None:
    """Kill-switch (``[display] math_rendering = false`` in velune.toml)."""
    global _ENABLED
    _ENABLED = bool(value)


def enabled() -> bool:
    return _ENABLED


_TRAILING_JUNK = re.compile(r"(\\(?:begin|end)\{[^}]*|\\[A-Za-z]*|\\\\|[&^_\s])$")
_STRUCTURE = re.compile(r"\\[{}]|\\begin\{([^}]*)\}|\\end\{[^}]*\}|\\left\b|\\right\b|[{}]")


def close_partial(tex: str) -> str:
    """Provisionally complete a formula that is still streaming in.

    Drops a half-typed trailing command (``\\begin{bmat``, ``\\fr``, a dangling
    ``&`` or row break) and closes whatever groups, environments and
    ``\\left`` delimiters are still open, so the part that *has* arrived can
    be laid out. Purely cosmetic: the final render uses the real text.
    """
    prev = None
    while prev != tex:
        prev, tex = tex, _TRAILING_JUNK.sub("", tex)
    stack: list[str] = []
    for m in _STRUCTURE.finditer(tex):
        tok = m.group(0)
        if tok in ("\\{", "\\}"):
            continue
        if tok == "{":
            stack.append("}")
        elif tok == "\\left":
            stack.append("\\right.")
        elif m.group(1) is not None:
            stack.append(f"\\end{{{m.group(1)}}}")
        elif stack:  # "}", "\right" or "\end{…}" closes the innermost
            stack.pop()
    return tex + "".join(reversed(stack))


@lru_cache(maxsize=512)
def render_inline(tex: str, ascii_only: bool = False) -> str | None:
    """One-line form of *tex*, or None if it can't be parsed."""
    try:
        return _linear.render(parse(tex), ascii_only=ascii_only)
    except (MathParseError, RecursionError) as exc:
        logger.debug("inline math not rendered (%s): %r", exc, tex[:80])
        return None
    except Exception as exc:  # never let presentation crash a response
        logger.debug("inline math renderer failed (%s): %r", exc, tex[:80])
        return None


@lru_cache(maxsize=256)
def render_display(tex: str, max_width: int = 80, ascii_only: bool = False) -> list[str] | None:
    """Multi-line layout of *tex* within *max_width* cells, or None if unparseable.

    Too wide for the terminal → the one-line form (the caller wraps it).
    """
    try:
        tree = parse(tex)
        box = _layout.layout(tree, ascii_only=ascii_only)
        if box.width <= max_width:
            return [line.rstrip() for line in box.lines]
        return [_linear.render(tree, ascii_only=ascii_only)]
    except (MathParseError, RecursionError) as exc:
        logger.debug("display math not rendered (%s): %r", exc, tex[:80])
        return None
    except Exception as exc:
        logger.debug("display math renderer failed (%s): %r", exc, tex[:80])
        return None
