"""One-line rendering of a math tree, for inline math (``$x^2$`` → ``x²``).

Also the fallback for display math that is too wide for the terminal.
"""

from __future__ import annotations

import re

from velune.cli.rendering.math import symbols
from velune.cli.rendering.math.spacing import spaced
from velune.cli.rendering.math.tex import (
    Accent,
    BigOp,
    Delim,
    Frac,
    Matrix,
    Node,
    Raw,
    Row,
    Script,
    Space,
    Sqrt,
    Sym,
    TextNode,
)

_SIMPLE = re.compile(r"^[\w.′'·⁰-⁹ᵃ-ᶻ]+$")
_ENV_BRACKETS = {
    "pmatrix": ("(", ")"),
    "bmatrix": ("[", "]"),
    "Bmatrix": ("{", "}"),
    "vmatrix": ("|", "|"),
    "Vmatrix": ("‖", "‖"),
}


def render(node: Node, ascii_only: bool = False) -> str:
    return _r(node, ascii_only).strip()


def _wrap(text: str) -> str:
    """Parenthesise a multi-token operand: `a+b` → `(a+b)`; `ab`/`2`/`x²` stay bare."""
    return text if _SIMPLE.match(text) else f"({text})"


def _script(text: str, sup: bool, ascii_only: bool) -> str:
    if not ascii_only:
        converted = symbols.to_superscript(text) if sup else symbols.to_subscript(text)
        if converted is not None:
            return converted
    mark = "^" if sup else "_"
    return f"{mark}{text}" if len(text) == 1 else f"{mark}({text})"


def tight(node: Node, a: bool) -> str:
    """Scripts and limits are set without operator spacing (``k=1``, not ``k = 1``)."""
    if isinstance(node, Row):
        return "".join(_r(item, a) for item in node.items if not isinstance(item, Space))
    return _r(node, a)


def _r(node: Node, a: bool) -> str:
    if isinstance(node, Sym):
        return node.ascii if a else node.uni
    if isinstance(node, Space):
        return " " * node.width
    if isinstance(node, Raw):
        return node.text
    if isinstance(node, TextNode):
        return node.text
    if isinstance(node, Row):
        return "".join(_r(item, a) for item in spaced(node.items))
    if isinstance(node, Script):
        out = _r(node.base, a)
        if node.sub is not None:
            out += _script(tight(node.sub, a), sup=False, ascii_only=a)
        if node.sup is not None:
            out += _script(tight(node.sup, a), sup=True, ascii_only=a)
        return out
    if isinstance(node, Frac):
        num, den = _r(node.num, a), _r(node.den, a)
        if not node.rule:
            return f"C({num}, {den})"
        return f"{_wrap(num)}/{_wrap(den)}"
    if isinstance(node, Sqrt):
        body = _r(node.body, a)
        if a:
            index = f"{_r(node.index, a)}, " if node.index is not None else ""
            return f"sqrt({index}{body})"
        root = "√"
        if node.index is not None:
            idx = _r(node.index, a)
            root = {"3": "∛", "4": "∜"}.get(idx) or ((symbols.to_superscript(idx) or idx) + "√")
        return root + (body if _SIMPLE.match(body) else f"({body})")
    if isinstance(node, BigOp):
        out = node.ascii if a else node.uni
        if node.sub is not None or node.sup is not None:
            sub = tight(node.sub, a) if node.sub is not None else ""
            sup = tight(node.sup, a) if node.sup is not None else ""
            if not a:
                sub_s = symbols.to_subscript(sub) if sub else ""
                sup_s = symbols.to_superscript(sup) if sup else ""
                if sub_s is not None and sup_s is not None:
                    return out + sub_s + sup_s
            if sub and sup:
                return f"{out}[{sub}..{sup}]" if node.limits else f"{out}_{_wrap(sub)}^{_wrap(sup)}"
            if sub:
                return f"{out}_{_wrap(sub)}" if not node.limits else f"{out}[{sub}]"
            return f"{out}^{_wrap(sup)}"
        return out
    if isinstance(node, Delim):
        if isinstance(node.body, Frac) and not node.body.rule:  # inom → C(n, k)
            return _r(node.body, a)
        return f"{node.left}{_r(node.body, a)}{node.right}"
    if isinstance(node, Accent):
        body = _r(node.body, a)
        if not a and len(body) == 1:
            return body + node.uni
        return f"{node.ascii}({body})"
    if isinstance(node, Matrix):
        return _matrix(node, a)
    return ""


def _matrix(node: Matrix, a: bool) -> str:
    rows = [[_r(cell, a).strip() for cell in row] for row in node.rows]
    if node.env in ("cases", "dcases", "rcases"):
        parts = [" if ".join(c for c in row if c) if len(row) > 1 else row[0] for row in rows]
        return "{ " + "; ".join(parts) + " }"
    if node.env.startswith(("align", "gather", "split", "eqnarray")):
        return "; ".join(" ".join(c for c in row if c) for row in rows)
    body = "; ".join(" ".join(row) for row in rows)
    left, right = _ENV_BRACKETS.get(node.env, ("[", "]"))
    if a:
        left, right = {"‖": "||"}.get(left, left), {"‖": "||"}.get(right, right)
    return f"{left}{body}{right}"
