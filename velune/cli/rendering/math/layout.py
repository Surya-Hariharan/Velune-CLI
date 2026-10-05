"""Two-dimensional terminal layout of a math tree, for display math.

A ``Box`` is a block of text lines with a baseline row; boxes are joined
horizontally on their baselines and stacked vertically for fractions, limits
and matrices. Widths are measured in terminal cells (``rich.cells.cell_len``)
so wide/combining characters line up. Every construct has an ASCII form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.cells import cell_len

from velune.cli.rendering.math import linear, symbols
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


@dataclass
class Box:
    lines: list[str]
    baseline: int

    @property
    def width(self) -> int:
        return max((cell_len(line) for line in self.lines), default=0)

    @property
    def height(self) -> int:
        return len(self.lines)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - cell_len(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def atom(text: str) -> Box:
    return Box([text], 0)


def hjoin(boxes: list[Box]) -> Box:
    boxes = [b for b in boxes if b.lines]
    if not boxes:
        return atom("")
    above = max(b.baseline for b in boxes)
    below = max(b.height - b.baseline - 1 for b in boxes)
    height = above + below + 1
    lines = [""] * height
    for box in boxes:
        width = box.width
        top = above - box.baseline
        for row in range(height):
            src = row - top
            text = box.lines[src] if 0 <= src < box.height else ""
            lines[row] += _pad(text, width)
    return Box([line.rstrip() for line in lines], above)


def vstack(boxes: list[Box], baseline_row: int, align: str = "center") -> Box:
    width = max((b.width for b in boxes), default=0)
    lines: list[str] = []
    for box in boxes:
        lines.extend(_pad(line, width, align) for line in box.lines)
    return Box(lines, baseline_row)


# ── Glyph sets ───────────────────────────────────────────────────────────────

# delimiter → (single, top, middle, bottom, extension)
_TALL_UNI = {
    "(": ("(", "⎛", "⎜", "⎝", "⎜"),
    ")": (")", "⎞", "⎟", "⎠", "⎟"),
    "[": ("[", "⎡", "⎢", "⎣", "⎢"),
    "]": ("]", "⎤", "⎥", "⎦", "⎥"),
    "{": ("{", "⎧", "⎨", "⎩", "⎪"),
    "}": ("}", "⎫", "⎬", "⎭", "⎪"),
    "|": ("|", "│", "│", "│", "│"),
    "‖": ("‖", "‖", "‖", "‖", "‖"),
    "⌊": ("⌊", "│", "│", "⌊", "│"),
    "⌋": ("⌋", "│", "│", "⌋", "│"),
    "⌈": ("⌈", "⌈", "│", "│", "│"),
    "⌉": ("⌉", "⌉", "│", "│", "│"),
    "⟨": ("⟨", "⟨", "⟨", "⟨", "⟨"),
    "⟩": ("⟩", "⟩", "⟩", "⟩", "⟩"),
}
_TALL_ASCII = {
    "(": ("(", "/", "|", "\\", "|"),
    ")": (")", "\\", "|", "/", "|"),
    "[": ("[", "[", "[", "[", "["),
    "]": ("]", "]", "]", "]", "]"),
    "{": ("{", "/", "<", "\\", "|"),
    "}": ("}", "\\", ">", "/", "|"),
    "|": ("|", "|", "|", "|", "|"),
    "‖": ("||", "||", "||", "||", "||"),
}

_ENV_DELIMS = {
    "matrix": ("", ""),
    "smallmatrix": ("", ""),
    "pmatrix": ("(", ")"),
    "bmatrix": ("[", "]"),
    "Bmatrix": ("{", "}"),
    "vmatrix": ("|", "|"),
    "Vmatrix": ("‖", "‖"),
    "array": ("", ""),
}

_NUMBER = re.compile(r"^[−\-+]?\d[\d.,]*$")


def tall_delim(char: str, height: int, baseline: int, ascii_only: bool) -> Box:
    if not char:
        return Box([""] * height, baseline)
    table = _TALL_ASCII if ascii_only else _TALL_UNI
    if ascii_only:
        char = {"⌊": "|", "⌋": "|", "⌈": "|", "⌉": "|", "⟨": "<", "⟩": ">"}.get(char, char)
    glyphs = table.get(char)
    if glyphs is None:
        return Box([char] * height, baseline)
    single, top, mid, bottom, ext = glyphs
    if height == 1:
        return Box([single], 0)
    lines = [top] + [ext] * (height - 2) + [bottom]
    if char in "{}" and height >= 3:  # brace: point at the middle row
        lines = [top] + [ext] * (height - 2) + [bottom]
        lines[height // 2] = mid
    elif char in "{}":  # 2 rows: use plain pieces
        lines = [top, bottom]
    return Box(lines, baseline)


# ── Layout ───────────────────────────────────────────────────────────────────


def layout(node: Node, ascii_only: bool = False) -> Box:
    return _lay(node, ascii_only)


def _lin(node: Node, a: bool) -> str:
    return linear.render(node, ascii_only=a)


def _lay(node: Node, a: bool) -> Box:
    if isinstance(node, (Sym, Raw, TextNode, Space)):
        return atom(linear._r(node, a))
    if isinstance(node, Row):
        boxes: list[Box] = []
        for item in spaced(node.items):
            box = _lay(item, a)
            # Two tall pieces side by side (a fraction then a matrix) need a gap.
            if boxes and box.height > 1 and boxes[-1].height > 1:
                boxes.append(atom(" "))
            boxes.append(box)
        return hjoin(boxes)
    if isinstance(node, Script):
        return _script(node, a)
    if isinstance(node, Frac):
        return _frac(node, a)
    if isinstance(node, Sqrt):
        return _sqrt(node, a)
    if isinstance(node, BigOp):
        return _bigop(node, a)
    if isinstance(node, Delim):
        body = _lay(node.body, a)
        left = tall_delim(node.left, body.height, body.baseline, a)
        right = tall_delim(node.right, body.height, body.baseline, a)
        return hjoin([left, body, right])
    if isinstance(node, Accent):
        return atom(linear._r(node, a))
    if isinstance(node, Matrix):
        return _matrix(node, a)
    return atom("")


def _tight(node: Node, a: bool) -> Box:
    """Scripts and limits: no operator spacing (TeX sets them tight)."""
    if isinstance(node, Row):
        return hjoin([_lay(item, a) for item in node.items if not isinstance(item, Space)])
    return _lay(node, a)


def _is_flat(box: Box) -> bool:
    return box.height == 1


def _script(node: Script, a: bool) -> Box:
    base = _lay(node.base, a)
    sup = _tight(node.sup, a) if node.sup is not None else None
    sub = _tight(node.sub, a) if node.sub is not None else None
    # Prefer Unicode scripts on a single line when every piece fits.
    if _is_flat(base) and (sup is None or _is_flat(sup)) and (sub is None or _is_flat(sub)):
        flat = linear._r(node, a)
        if a or ("^" not in flat and "_" not in flat):
            return atom(flat)
    # Otherwise raise/lower the scripts next to the base.
    width = max(sup.width if sup else 0, sub.width if sub else 0)
    column: list[str] = []
    if sup is not None:
        column.extend(sup.lines)
    column.extend([""] * base.height)
    if sub is not None:
        column.extend(sub.lines)
    top = sup.height if sup is not None else 0
    lines: list[str] = []
    for row, script_line in enumerate(column):
        base_row = row - top
        base_text = base.lines[base_row] if 0 <= base_row < base.height else ""
        lines.append(_pad(base_text, base.width) + _pad(script_line, width))
    return Box([line.rstrip() for line in lines], top + base.baseline)


def _frac(node: Frac, a: bool) -> Box:
    num, den = _lay(node.num, a), _lay(node.den, a)
    width = max(num.width, den.width) + (2 if node.rule else 0)
    rule = ("-" if a else "─") * width if node.rule else " " * width
    lines = [_pad(line, width, "center") for line in num.lines]
    lines.append(rule)
    lines.extend(_pad(line, width, "center") for line in den.lines)
    return Box(lines, num.height)


def _sqrt(node: Sqrt, a: bool) -> Box:
    body = _lay(node.body, a)
    index = linear.render(node.index, a) if node.index is not None else ""
    if a:
        inner = hjoin([atom("sqrt("), body, atom(")")])
        return hjoin([atom(f"[{index}]"), inner]) if index else inner
    root = "√"
    if index:
        root = {"3": "∛", "4": "∜"}.get(index) or ((symbols.to_superscript(index) or index) + "√")
    bar = " " * cell_len(root) + "─" * (body.width + 1)
    if body.height == 1:
        return Box([bar, root + " " + body.lines[0]], 1)
    lines = [bar]
    for i, line in enumerate(body.lines):
        prefix = root if i == body.height - 1 else " " * (cell_len(root) - 1) + "│"
        lines.append(prefix + " " + line)
    return Box(lines, body.baseline + 1)


def _bigop(node: BigOp, a: bool) -> Box:
    sym = atom(node.ascii if a else node.uni)
    sub = _tight(node.sub, a) if node.sub is not None else None
    sup = _tight(node.sup, a) if node.sup is not None else None
    if sub is None and sup is None:
        return sym
    if not node.limits:  # integrals: limits as scripts on the right
        return _script(Script(Sym(node.uni, node.ascii, "ord"), node.sup, node.sub), a)
    parts = []
    if sup is not None:
        parts.append(sup)
    parts.append(sym)
    if sub is not None:
        parts.append(sub)
    return vstack(parts, baseline_row=sup.height if sup is not None else 0)


def _cell_align(env: str, col: int, text: str, colspec: str) -> str:
    if colspec and col < len(colspec):
        return {"l": "left", "r": "right", "c": "center"}.get(colspec[col], "center")
    if env.startswith(("align", "split", "eqnarray")):
        return "right" if col % 2 == 0 else "left"
    if env in ("cases", "dcases", "rcases", "gather", "gather*", "gathered"):
        return "left"
    return "right" if _NUMBER.match(text.strip()) else "center"


def _matrix(node: Matrix, a: bool) -> Box:
    if not node.rows:
        return atom("")
    grid = [[_lay(cell, a) for cell in row] for row in node.rows]
    ncols = max(len(row) for row in grid)
    for row in grid:
        row.extend(atom("") for _ in range(ncols - len(row)))
    widths = [max(row[c].width for row in grid) for c in range(ncols)]
    aligned = node.env.startswith(("align", "split", "eqnarray"))
    gap = " " if aligned else ("   " if node.env in ("cases", "dcases", "rcases") else "  ")
    tall = any(cell.height > 1 for row in grid for cell in row)

    lines: list[str] = []
    for r, row in enumerate(grid):
        height = max(cell.height for cell in row)
        base = max(cell.baseline for cell in row)
        for line_no in range(height):
            parts = []
            for c, cell in enumerate(row):
                src = line_no - (base - cell.baseline)
                text = cell.lines[src] if 0 <= src < cell.height else ""
                align = _cell_align(
                    node.env,
                    c,
                    linear._r(node.rows[r][c], a) if c < len(node.rows[r]) else "",
                    node.colspec,
                )
                parts.append(_pad(text, widths[c], align))
            lines.append(gap.join(parts).rstrip())
        if tall and r < len(grid) - 1:
            lines.append("")
    body = Box(lines, (len(lines) - 1) // 2)

    if node.env in ("cases", "dcases"):
        return hjoin([tall_delim("{", body.height, body.baseline, a), atom(" "), body])
    if node.env == "rcases":
        return hjoin([body, atom(" "), tall_delim("}", body.height, body.baseline, a)])
    left, right = _ENV_DELIMS.get(node.env, ("", ""))
    if not left and not right:
        return body
    pad_left = atom(" ")
    return hjoin(
        [
            tall_delim(left, body.height, body.baseline, a),
            pad_left,
            body,
            atom(" "),
            tall_delim(right, body.height, body.baseline, a),
        ]
    )
