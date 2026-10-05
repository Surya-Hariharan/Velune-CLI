"""Find math in a Markdown response before Markdown parsing can destroy it.

CommonMark treats ``\\[`` ``\\(`` and ``\\\\`` as backslash *escapes*, so by
the time a Markdown renderer runs, ``\\[ A=\\begin{bmatrix} 1 & 2 \\\\ 3 & 4
\\end{bmatrix} \\]`` has become ``[ A=\\begin{bmatrix} 1 & 2 \\ 3 & 4 …]`` —
the delimiters and row breaks are gone. This pre-pass runs on the raw text:

* display math (``$$…$$``, ``\\[…\\]``, a bare ``\\begin{bmatrix}…\\end{…}``
  paragraph) becomes a ```` ```velune-math ```` fence holding the original TeX,
  rendered later by ``CustomCodeBlock``;
* inline math (``$…$``, ``\\(…\\)``, a lone known symbol such as ``\\cdot`` in
  prose) is rendered to one line and substituted, Markdown-escaped;
* fenced code and inline code spans are never touched.

``$`` follows Pandoc's rules — no space just inside either delimiter and no
digit right after the closing ``$`` — so ``costs $5 and $10`` stays prose.
"""

from __future__ import annotations

import re

from velune.cli.rendering.math import render_inline, symbols
from velune.cli.rendering.math.tex import MATRIX_ENVS

MATH_FENCE = "velune-math"
PENDING_FENCE = "velune-math-pending"

_FENCE_OPEN = re.compile(r"^(\s{0,3})(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)", re.S)
_BEGIN = re.compile(r"\\begin\{([A-Za-z]+\*?)\}")
_DISPLAY_ENVS = MATRIX_ENVS | {"equation", "equation*", "multline", "multline*"}
# A lone symbol command in prose: not part of a path or word (C:\alpha\x is a path).
_BARE_SYMBOL = re.compile(r"(?<![\w:\\/])\\([A-Za-z]+)(?![A-Za-z])")
_SAFE_BARE = symbols.symbol_names()
# Text before a bare \begin{…} on the same line that belongs to the formula ("A = ").
_MATHY_PREFIX = re.compile(r"^[\s>]*(?:[-*+]\s+|\d+[.)]\s+)?(?P<expr>[^\s].{0,40}?=\s*)$")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~])")


def _looks_like_math(tex: str) -> bool:
    """Guard for ``\\[…\\]``: ``\\[1\\]`` is an escaped citation, not math."""
    return bool(re.search(r"[\\^_=+\-*/<>]|[A-Za-z]\s*[A-Za-z0-9]", tex.strip()))


def _escape_md(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def _inline(tex: str, source: str, ascii_only: bool) -> str:
    rendered = render_inline(tex.strip(), ascii_only)
    if rendered is None:
        # Unparseable: show the original text intact (escaped so Markdown
        # doesn't eat its backslashes) rather than a mangled half-render.
        return _escape_md(source)
    return _escape_md(rendered)


# ── Block structure: protect fenced code ─────────────────────────────────────


def _split_fences(text: str) -> list[tuple[bool, str]]:
    """``(is_code, chunk)`` pieces; code fences are kept verbatim."""
    pieces: list[tuple[bool, str]] = []
    buf: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        if fence is None:
            m = _FENCE_OPEN.match(line)
            if m:
                if buf:
                    pieces.append((False, "\n".join(buf)))
                    buf = []
                fence = m.group(2)
                buf.append(line)
                continue
            buf.append(line)
        else:
            buf.append(line)
            stripped = line.strip()
            if stripped.startswith(fence[0] * len(fence)) and not stripped.strip(fence[0]):
                pieces.append((True, "\n".join(buf)))
                buf = []
                fence = None
    if buf:
        pieces.append((fence is not None, "\n".join(buf)))
    return pieces


def ends_inside_code_fence(text: str) -> bool:
    pieces = _split_fences(text)
    return bool(pieces) and pieces[-1][0] and not _fence_closed(pieces[-1][1])


def _fence_closed(chunk: str) -> bool:
    lines = chunk.split("\n")
    if len(lines) < 2:
        return False
    m = _FENCE_OPEN.match(lines[0])
    if not m:
        return False
    fence = m.group(2)
    last = lines[-1].strip()
    return last.startswith(fence[0] * len(fence)) and not last.strip(fence[0])


# ── Display math ─────────────────────────────────────────────────────────────


def _find_env_end(text: str, start: int, env: str) -> int:
    """Index just past the matching ``\\end{env}`` (nesting-aware), or -1."""
    depth, pos = 1, start
    begin_tok, end_tok = f"\\begin{{{env}}}", f"\\end{{{env}}}"
    while depth:
        nb = text.find(begin_tok, pos)
        ne = text.find(end_tok, pos)
        if ne == -1:
            return -1
        if nb != -1 and nb < ne:
            depth += 1
            pos = nb + len(begin_tok)
        else:
            depth -= 1
            pos = ne + len(end_tok)
    return pos


def _scan_display(chunk: str) -> list[tuple[str, str]]:
    """Split a prose chunk into ``("text", s)``, ``("display", tex)``, ``("pending", tex)``."""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(chunk)

    def flush() -> None:
        if buf:
            out.append(("text", "".join(buf)))
            buf.clear()

    while i < n:
        ch = chunk[i]
        if ch == "`":
            m = _CODE_SPAN.match(chunk, i)
            if m:
                buf.append(m.group(0))
                i = m.end()
                continue
            j = i
            while j < n and chunk[j] == "`":
                j += 1
            buf.append(chunk[i:j])
            i = j
            continue
        if chunk.startswith("\\$", i):
            buf.append("\\$")
            i += 2
            continue
        if chunk.startswith("$$", i):
            end = chunk.find("$$", i + 2)
            if end == -1:
                flush()
                out.append(("pending", chunk[i + 2 :]))
                return out
            flush()
            out.append(("display", chunk[i + 2 : end]))
            i = end + 2
            continue
        if chunk.startswith("\\[", i):
            end = chunk.find("\\]", i + 2)
            if end == -1:
                if _looks_like_math(chunk[i + 2 :]):
                    flush()
                    out.append(("pending", chunk[i + 2 :]))
                    return out
            elif _looks_like_math(chunk[i + 2 : end]):
                flush()
                out.append(("display", chunk[i + 2 : end]))
                i = end + 2
                continue
        # A closed inline span is the inline pass's business, even when it holds
        # an environment (\(A=\begin{pmatrix}…\end{pmatrix}\)): skip it whole.
        if chunk.startswith("\\(", i):
            end = chunk.find("\\)", i + 2)
            if end != -1 and "\n\n" not in chunk[i:end]:
                buf.append(chunk[i : end + 2])
                i = end + 2
                continue
        if ch == "$":
            end = _dollar_close(chunk, i + 1)
            if end != -1:
                buf.append(chunk[i : end + 1])
                i = end + 1
                continue
        m = _BEGIN.match(chunk, i)
        if m and m.group(1) in _DISPLAY_ENVS:
            env = m.group(1)
            end = _find_env_end(chunk, m.end(), env)
            # Pull a math-looking lead-in on the same line ("A = ") into the formula.
            joined = "".join(buf)
            line_start = joined.rfind("\n") + 1
            lead = joined[line_start:]
            pm = _MATHY_PREFIX.match(lead)
            prefix = ""
            if pm:
                prefix = pm.group("expr")
                joined = joined[:line_start] + lead[: pm.start("expr")]
                buf.clear()
                buf.append(joined)
            if end == -1:
                flush()
                out.append(("pending", prefix + chunk[i:]))
                return out
            flush()
            out.append(("display", prefix + chunk[i:end]))
            i = end
            continue
        buf.append(ch)
        i += 1
    flush()
    return out


# ── Inline math ──────────────────────────────────────────────────────────────


def _dollar_close(text: str, start: int) -> int:
    """Pandoc rules for a closing ``$`` after an opening one at ``start - 1``."""
    if start >= len(text) or text[start].isspace():
        return -1
    pos = start
    while True:
        k = text.find("$", pos)
        if k == -1 or "\n\n" in text[start:k]:
            return -1
        if text[k - 1] == "\\":
            pos = k + 1
            continue
        if not text[k - 1].isspace() and not (k + 1 < len(text) and text[k + 1].isdigit()):
            return k
        pos = k + 1


def _replace_inline(text: str, ascii_only: bool) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "`":
            m = _CODE_SPAN.match(text, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
            j = i
            while j < n and text[j] == "`":
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("\\$", i):
            out.append("\\$")
            i += 2
            continue
        if text.startswith("\\(", i):
            end = text.find("\\)", i + 2)
            if end != -1 and "\n\n" not in text[i:end]:
                out.append(_inline(text[i + 2 : end], text[i : end + 2], ascii_only))
                i = end + 2
                continue
        if ch == "$" and not text.startswith("$$", i):
            end = _dollar_close(text, i + 1)
            if end != -1:
                out.append(_inline(text[i + 1 : end], text[i : end + 1], ascii_only))
                i = end + 1
                continue
        if ch == "\\":
            m = _BARE_SYMBOL.match(text, i)
            if m and m.group(1) in _SAFE_BARE and (i == 0 or not _BARE_PREV.match(text[i - 1])):
                found = symbols.lookup(m.group(1))
                if found is not None:
                    out.append(_escape_md(found[1] if ascii_only else found[0]))
                    i = m.end()
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


_BARE_PREV = re.compile(r"[\w:\\/]")


# ── Public API ───────────────────────────────────────────────────────────────


def _fence(tex: str, indent: str, kind: str = MATH_FENCE) -> str:
    body = "\n".join(indent + line for line in tex.strip("\n").split("\n"))
    return f"\n\n{indent}```{kind}\n{body}\n{indent}```\n\n"


def _indent_of(text: str) -> str:
    """Leading whitespace if *text* ends with an otherwise-empty line (list continuation)."""
    last = text[text.rfind("\n") + 1 :]
    return last if last.strip() == "" else ""


def has_math(text: str) -> bool:
    return any(token in text for token in ("$", "\\[", "\\(", "\\begin{", "\\"))


def transform(text: str, ascii_only: bool = False, *, streaming: bool = False) -> str:
    """Rewrite *text* so math survives Markdown and renders in the terminal.

    ``streaming=True``: an unclosed display formula at the end (the model is
    still typing it) becomes a pending block instead of raw source.
    """
    if not text or not has_math(text):
        return text
    text = text.replace("\r\n", "\n")
    chunks: list[str] = []
    for is_code, chunk in _split_fences(text):
        if is_code:
            chunks.append(chunk)
            continue
        parts: list[str] = []
        for kind, part in _scan_display(chunk):
            if kind == "text":
                parts.append(_replace_inline(part, ascii_only))
            elif kind == "display":
                # Inside a list item the formula keeps the item's indentation.
                indent = _indent_of("".join(parts))
                if indent and parts:
                    parts[-1] = parts[-1][: len(parts[-1]) - len(indent)]
                parts.append(_fence(part, indent))
            elif streaming:
                parts.append(_fence(part or " ", "", PENDING_FENCE))
            else:
                # An unclosed formula in a finished response: show it as written.
                opener = "" if part.lstrip().startswith("\\begin") else "$$"
                parts.append(_escape_md(opener + part))
        chunks.append("".join(parts))
    # _split_fences cut the text at line boundaries; restore them.
    return "\n".join(chunks)
