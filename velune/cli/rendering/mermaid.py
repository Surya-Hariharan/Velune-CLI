"""Terminal rendering for Mermaid flowcharts.

A terminal can't draw Mermaid, but the common case (``graph TD`` / ``flowchart
LR`` with ``A --> B`` edges) reads fine as a box-drawing tree. Anything this
module doesn't understand returns ``None`` so the caller can fall back to
showing the source as a normal code block.
"""

from __future__ import annotations

import re

from rich.text import Text

from velune.cli import design

_HEADER = re.compile(r"^(?:graph|flowchart)\b", re.IGNORECASE)
# Flowchart link operators, longest first so "-.->" wins over "---" at the same spot.
_ARROWS = ("-" * 2 + ">", "-" * 3, "-." + "-" + ">", "=" * 2 + ">", "--o", "--x")
_NODE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s*"
    r"(?:\[\[?(.*?)\]?\]|\(\((.*?)\)\)|\((.*?)\)|\{(.*?)\}|>(.*?)\])?\s*$"
)
_MAX_NODES = 60


def _split_chain(stmt: str) -> tuple[list[str], list[str]]:
    """Split ``A --> B -->|text| C`` into node tokens and the labels between them."""
    nodes: list[str] = []
    labels: list[str] = []
    rest = stmt
    while True:
        found = [(rest.find(a), a) for a in _ARROWS if a in rest]
        if not found:
            nodes.append(rest)
            return nodes, labels
        # earliest operator; prefer the longest when several start at the same index
        pos, arrow = min(found, key=lambda f: (f[0], -len(f[1])))
        nodes.append(rest[:pos])
        rest = rest[pos + len(arrow) :].lstrip()
        label = ""
        if rest.startswith("|") and "|" in rest[1:]:
            end = rest.index("|", 1)
            label, rest = rest[1:end], rest[end + 1 :]
        labels.append(label)


def _parse(source: str) -> tuple[dict[str, str], list[tuple[str, str, str]]] | None:
    statements = [s.strip() for s in re.split(r"[;\n]", source) if s.strip()]
    if not statements or not _HEADER.match(statements[0]):
        return None
    labels: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []

    def node(token: str) -> str | None:
        m = _NODE.match(token)
        if not m:
            return None
        nid = m.group(1)
        label = next((g for g in m.groups()[1:] if g), None)
        if label:
            labels[nid] = label.strip().strip('"[]()')
        labels.setdefault(nid, nid)
        return nid

    for stmt in statements[1:]:
        if stmt.lower().startswith(("subgraph", "end", "style", "classdef", "class ", "click")):
            continue
        node_tokens, arrow_labels = _split_chain(stmt)
        ids = [node(t) for t in node_tokens]
        if any(i is None for i in ids):
            return None
        for idx in range(len(ids) - 1):
            edges.append((ids[idx], ids[idx + 1], (arrow_labels[idx] or "").strip()))  # type: ignore[arg-type]
    if not edges or len(labels) > _MAX_NODES:
        return None
    return labels, edges


def render_mermaid(source: str, width: int) -> Text | None:
    """Return the flowchart as a tree, or None if it isn't a supported flowchart."""
    parsed = _parse(source)
    if parsed is None:
        return None
    labels, edges = parsed

    children: dict[str, list[tuple[str, str]]] = {}
    incoming: set[str] = set()
    for src, dst, text in edges:
        children.setdefault(src, []).append((dst, text))
        incoming.add(dst)
    roots = [n for n in labels if n not in incoming] or [next(iter(labels))]

    lines: list[Text] = []
    shown: set[str] = set()

    def emit(nid: str, prefix: str, connector: str, edge_text: str, ancestors: set[str]) -> None:
        label = labels[nid]
        seen = nid in ancestors or nid in shown
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(prefix + connector, style=design.FAINT)
        if edge_text:
            line.append(f"{edge_text} ", style=design.MUTED)
        line.append(f"[{label}]", style=f"bold {design.INFO}" if not seen else design.MUTED)
        if seen:
            line.append(" ↩", style=design.MUTED)
        lines.append(line)
        if seen:
            return
        shown.add(nid)
        kids = children.get(nid, [])
        for i, (kid, text) in enumerate(kids):
            last = i == len(kids) - 1
            child_prefix = prefix + ("    " if connector == "└─▶ " else "│   " if connector else "")
            emit(kid, child_prefix, "└─▶ " if last else "├─▶ ", text, ancestors | {nid})

    for root in roots:
        emit(root, "", "", "", set())

    out = Text(no_wrap=True, overflow="ellipsis")
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        line.truncate(max(width, 10), overflow="ellipsis")
        out.append_text(line)
    return out
