"""Shared horizontal spacing rules for the linear and 2D renderers."""

from __future__ import annotations

from velune.cli.rendering.math.tex import BigOp, Node, Space, Sym


def _kind(node: Node | None) -> str:
    if node is None:
        return "start"
    if isinstance(node, Sym):
        return node.kind
    if isinstance(node, BigOp):
        return "op"
    return "ord"


def spaced(items: list[Node]) -> list[Node]:
    """Insert spaces around binary operators and relations.

    A ``-``/``+`` at the start, after an opening bracket, an operator, a
    relation or punctuation is a sign (``-1``, ``= -b``), so it is not spaced.
    Function words (``sin``, ``lim``) get a trailing space before an operand.
    """
    out: list[Node] = []
    prev: Node | None = None
    for item in items:
        if isinstance(item, Space):
            out.append(item)
            continue
        kind = _kind(item)
        prev_kind = _kind(prev)
        if kind == "bin":
            unary = prev_kind in ("start", "open", "bin", "rel", "punct", "op")
            if unary:
                out.append(item)
            else:
                out.extend([Space(1), item, Space(1)])
        elif kind == "rel":
            if prev_kind == "rel" and out and isinstance(out[-1], Space) and out[-2] is prev:
                out.pop()  # adjacent relations join: ":=", "<=" written as two tokens
            elif prev_kind != "start":
                out.append(Space(1))
            out.append(item)
            out.append(Space(1))
        elif kind == "punct" and isinstance(item, Sym) and item.uni in ",;":
            out.extend([item, Space(1)])
        else:
            if prev_kind in ("word", "op") and kind not in ("open", "close", "punct"):
                out.append(Space(1))
            out.append(item)
        prev = item
    # Collapse runs of spaces and trim the ends.
    collapsed: list[Node] = []
    for node in out:
        if isinstance(node, Space) and collapsed and isinstance(collapsed[-1], Space):
            collapsed[-1] = Space(max(collapsed[-1].width, node.width))
            continue
        collapsed.append(node)
    while collapsed and isinstance(collapsed[0], Space):
        collapsed.pop(0)
    while collapsed and isinstance(collapsed[-1], Space):
        collapsed.pop()
    return collapsed
