"""A small TeX math parser: tokenizer + recursive descent into a layout-neutral tree.

Covers what language models actually write: scripts, fractions, roots, big
operators with limits, ``\\left…\\right``, matrix/cases/aligned/array
environments, text, fonts, accents, spacing and symbols. Anything unknown is
kept as literal text (``Raw``) rather than failing; only structurally broken
input (unbalanced braces, an unclosed environment) raises ``MathParseError``,
which callers turn into a readable source fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from velune.cli.rendering.math import symbols


class MathParseError(ValueError):
    pass


# ── AST ──────────────────────────────────────────────────────────────────────


@dataclass
class Node:
    pass


@dataclass
class Sym(Node):
    """A glyph. ``kind``: ord / bin / rel / open / close / punct / num / var / word."""

    uni: str
    ascii: str
    kind: str = "ord"


@dataclass
class Row(Node):
    items: list[Node] = field(default_factory=list)


@dataclass
class Script(Node):
    base: Node
    sup: Node | None = None
    sub: Node | None = None


@dataclass
class Frac(Node):
    num: Node
    den: Node
    rule: bool = True  # False for \binom


@dataclass
class Sqrt(Node):
    body: Node
    index: Node | None = None


@dataclass
class BigOp(Node):
    uni: str
    ascii: str
    limits: bool
    sub: Node | None = None
    sup: Node | None = None


@dataclass
class Delim(Node):
    left: str  # "" for none
    right: str
    body: Node


@dataclass
class Matrix(Node):
    env: str
    rows: list[list[Node]]
    colspec: str = ""


@dataclass
class TextNode(Node):
    text: str


@dataclass
class Accent(Node):
    body: Node
    uni: str
    ascii: str


@dataclass
class Space(Node):
    width: int = 1


@dataclass
class Raw(Node):
    text: str


MATRIX_ENVS = frozenset(
    {
        "matrix",
        "pmatrix",
        "bmatrix",
        "Bmatrix",
        "vmatrix",
        "Vmatrix",
        "smallmatrix",
        "cases",
        "dcases",
        "rcases",
        "aligned",
        "align",
        "align*",
        "gather",
        "gather*",
        "gathered",
        "split",
        "array",
        "eqnarray",
        "eqnarray*",
        "alignat",
        "alignat*",
    }
)

_LITERAL_ESCAPES = {"{": "{", "}": "}", "%": "%", "$": "$", "_": "_", "&": "&", "#": "#", "|": "‖"}
_SPACES = {
    ",": 1,
    ":": 1,
    ";": 1,
    " ": 1,
    "quad": 2,
    "qquad": 4,
    "!": 0,
    "enspace": 1,
    "thinspace": 1,
}
_IGNORED = frozenset(
    {
        "displaystyle",
        "textstyle",
        "scriptstyle",
        "limits",
        "nolimits",
        "nonumber",
        "notag",
        # Table rules inside array/tabular: rows are already separated.
        "hline",
        "toprule",
        "midrule",
        "bottomrule",
        "big",
        "Big",
        "bigg",
        "Bigg",
        "bigl",
        "bigr",
        "Bigl",
        "Bigr",
        "biggl",
        "biggr",
    }
)
_FONTS = frozenset(
    {
        "mathrm",
        "mathbf",
        "mathit",
        "mathsf",
        "mathtt",
        "mathcal",
        "mathscr",
        "mathfrak",
        "boldsymbol",
        "bm",
        "textbf",
        "textit",
        "textrm",
        "emph",
    }
)
_DELIM_COMMANDS = {
    "langle": "⟨",
    "rangle": "⟩",
    "lvert": "|",
    "rvert": "|",
    "lVert": "‖",
    "rVert": "‖",
    "vert": "|",
    "Vert": "‖",
    "lfloor": "⌊",
    "rfloor": "⌋",
    "lceil": "⌈",
    "rceil": "⌉",
    "{": "{",
    "}": "}",
    "|": "‖",
    "lbrace": "{",
    "rbrace": "}",
}


# ── Tokenizer ────────────────────────────────────────────────────────────────


def tokenize(src: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            if i + 1 < n and src[i + 1].isalpha():
                j = i + 1
                while j < n and src[j].isalpha():
                    j += 1
                if j < n and src[j] == "*":  # \align*, \operatorname*
                    j += 1
                tokens.append(src[i:j])
                i = j
            elif i + 1 < n:
                tokens.append(src[i : i + 2])
                i += 2
            else:
                tokens.append("\\")
                i += 1
        elif ch.isspace():
            # Collapse whitespace into a single marker (significant in \text only).
            j = i
            while j < n and src[j].isspace():
                j += 1
            tokens.append(" ")
            i = j
        else:
            tokens.append(ch)
            i += 1
    return tokens


# ── Parser ───────────────────────────────────────────────────────────────────


class Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.toks = tokenize(src)
        self.pos = 0

    def peek(self) -> str | None:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def next(self) -> str:
        tok = self.peek()
        if tok is None:
            raise MathParseError("unexpected end of formula")
        self.pos += 1
        return tok

    def skip_space(self) -> None:
        while self.peek() == " ":
            self.pos += 1

    def parse(self) -> Row:
        row = self.parse_row(stop=frozenset())
        if self.peek() is not None:
            raise MathParseError(f"unexpected {self.peek()!r}")
        return row

    # A row ends at any token in `stop` (not consumed) or end of input.
    def parse_row(self, stop: frozenset[str]) -> Row:
        items: list[Node] = []
        while True:
            tok = self.peek()
            if tok is None or tok in stop:
                return Row(items)
            if tok == "}":
                if "}" in stop:
                    return Row(items)
                raise MathParseError("unbalanced '}'")
            if tok in ("^", "_"):
                self.next()
                base = items.pop() if items else Sym("", "")
                arg = self.parse_script_arg()
                items.append(self._attach(base, tok, arg))
                continue
            if tok == "'":  # prime
                self.next()
                base = items.pop() if items else Sym("", "")
                items.append(self._attach(base, "^", Sym("′", "'")))
                continue
            atom = self.parse_atom()
            if atom is not None:
                items.append(atom)

    def _attach(self, base: Node, op: str, arg: Node) -> Node:
        if isinstance(base, BigOp):
            if op == "^":
                base.sup = arg
            else:
                base.sub = arg
            return base
        if isinstance(base, Script) and (
            (op == "^" and base.sup is None) or (op == "_" and base.sub is None)
        ):
            if op == "^":
                base.sup = arg
            else:
                base.sub = arg
            return base
        return Script(base, arg, None) if op == "^" else Script(base, None, arg)

    def parse_script_arg(self) -> Node:
        self.skip_space()
        tok = self.peek()
        if tok == "{":
            return self.parse_group()
        if tok is None:
            raise MathParseError("missing script argument")
        atom = self.parse_atom()
        return atom if atom is not None else Row()

    def parse_group(self) -> Row:
        self.skip_space()
        if self.next() != "{":
            raise MathParseError("expected '{'")
        row = self.parse_row(stop=frozenset({"}"}))
        if self.peek() != "}":
            raise MathParseError("unclosed '{'")
        self.next()
        return row

    def parse_arg(self) -> Node:
        """A command argument: a {group} or a single token."""
        self.skip_space()
        if self.peek() == "{":
            return self.parse_group()
        tok = self.peek()
        if tok is not None and tok.isdigit():
            # An unbraced argument is one token: \frac12 is 1 over 2, not 12.
            self.pos += 1
            return Sym(tok, tok, "num")
        atom = self.parse_atom()
        if atom is None:
            raise MathParseError("missing argument")
        return atom

    def raw_group(self) -> str:
        """The verbatim text of a {group} (for \\text, \\begin names, colspecs)."""
        self.skip_space()
        if self.next() != "{":
            raise MathParseError("expected '{'")
        depth, parts = 1, []
        while True:
            tok = self.next()
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
                if depth == 0:
                    return "".join(parts)
            parts.append(tok)

    def raw_arg(self) -> str:
        """Verbatim argument: a {group}, or one token (TeX allows ``\\mathbb R``)."""
        self.skip_space()
        if self.peek() == "{":
            return self.raw_group()
        tok = self.next()
        if tok in ("}", "&"):
            raise MathParseError("missing argument")
        return tok

    def parse_atom(self) -> Node | None:
        tok = self.next()
        if tok == " ":
            return None
        if tok == "{":
            self.pos -= 1
            return self.parse_group()
        if tok.startswith("\\"):
            return self.parse_command(tok[1:])
        if tok.isdigit() or tok == ".":
            text = tok
            while (nxt := self.peek()) is not None and (nxt.isdigit() or nxt == "."):
                text += self.next()
            return Sym(text, text, "num")
        if tok.isalpha():
            return Sym(tok, tok, "var")
        if tok in "+-*/":
            uni = {"-": "−", "*": "∗"}.get(tok, tok)
            return Sym(uni, tok, "bin")
        if tok in "=<>:":  # ":" is a relation in TeX math (f : A → B)
            return Sym(tok, tok, "rel")
        if tok in "([":
            return Sym(tok, tok, "open")
        if tok in ")]":
            return Sym(tok, tok, "close")
        if tok in ",;!?":
            return Sym(tok, tok, "punct")
        if tok in ("&",):
            raise MathParseError("'&' outside an environment")
        return Sym(tok, tok, "ord")

    def parse_command(self, name: str) -> Node | None:
        if name in _LITERAL_ESCAPES and len(name) == 1:
            ch = _LITERAL_ESCAPES[name]
            return Sym(ch, "||" if ch == "‖" else ch, "ord")
        if name in _SPACES:
            return Space(_SPACES[name])
        if name in _IGNORED:
            return None
        if name in ("phantom", "hphantom", "vphantom"):
            # Invisible alignment spacer; columns are aligned by layout anyway.
            self.parse_arg()
            return None
        if name in ("smash", "boxed", "fbox", "underbrace", "overbrace"):
            # Keep the content; the decoration has no terminal equivalent worth
            # the noise (a highlighted answer reads fine without its box).
            return self.parse_arg()
        if name in ("label", "cline", "cmidrule"):
            self.raw_arg()
            return None
        if name == "tag":
            return TextNode(f"  ({self.raw_arg().strip()})")
        if name == "\\":
            raise MathParseError("'\\\\' outside an environment")
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            return Frac(self.parse_arg(), self.parse_arg())
        if name in ("binom", "dbinom", "tbinom"):
            return Delim("(", ")", Frac(self.parse_arg(), self.parse_arg(), rule=False))
        if name == "sqrt":
            index = None
            self.skip_space()
            if self.peek() == "[":
                self.next()
                index = self.parse_row(stop=frozenset({"]"}))
                if self.peek() != "]":
                    raise MathParseError("unclosed sqrt index")
                self.next()
            return Sqrt(self.parse_arg(), index)
        if name in symbols.BIG_OPS:
            uni, asc, limits = symbols.BIG_OPS[name]
            return BigOp(uni, asc, limits)
        if name in symbols.FUNCTIONS:
            return Sym(name, name, "word")
        if name in ("operatorname", "operatorname*"):
            return Sym(self.raw_arg().strip(), "", "word")
        if name in ("text", "textup", "textnormal", "mbox", "hbox"):
            return TextNode(self.raw_arg())
        if name == "mathbb":
            text = self.raw_arg().strip()
            return Sym("".join(symbols.BLACKBOARD.get(c, c) for c in text), text, "ord")
        if name in _FONTS:
            return self.parse_arg()
        if name in symbols.ACCENTS:
            uni, asc = symbols.ACCENTS[name]
            return Accent(self.parse_arg(), uni, asc)
        if name == "left":
            return self.parse_left_right()
        if name in ("right", "middle"):
            raise MathParseError(f"unmatched \\{name}")
        if name == "begin":
            return self.parse_environment()
        if name == "end":
            raise MathParseError("unmatched \\end")
        if name in ("not",):
            nxt = self.parse_arg()
            if isinstance(nxt, Sym) and nxt.uni == "=":
                return Sym("≠", "!=", "rel")
            return Row([Sym("¬", "not ", "ord"), nxt])
        if name in ("pmod",):
            return Row(
                [Space(1), Sym("(mod ", "(mod ", "ord"), self.parse_arg(), Sym(")", ")", "close")]
            )
        if name in _DELIM_COMMANDS:
            ch = _DELIM_COMMANDS[name]
            kind = "open" if name.startswith("l") or ch in "⟨⌊⌈{" else "close"
            return Sym(ch, ch, kind)
        found = symbols.lookup(name)
        if found is not None:
            uni, asc, kind = found
            return Sym(uni, asc, kind)
        # Unknown command: keep it readable rather than failing.
        return Raw("\\" + name)

    def _delim_token(self) -> str:
        self.skip_space()
        tok = self.next()
        if tok == ".":
            return ""
        if tok.startswith("\\"):
            return _DELIM_COMMANDS.get(tok[1:], tok[1:])
        return tok

    def parse_left_right(self) -> Node:
        left = self._delim_token()
        body = self.parse_row(stop=frozenset({"\\right"}))
        if self.peek() != "\\right":
            raise MathParseError("\\left without \\right")
        self.next()
        right = self._delim_token()
        return Delim(left, right, body)

    def parse_environment(self) -> Node:
        env = self.raw_group().strip()
        colspec = ""
        if env in ("array", "alignat", "alignat*"):
            colspec = self.raw_group().replace(" ", "").replace("|", "")
        if env not in MATRIX_ENVS:
            body = self.parse_row(stop=frozenset({"\\end"}))
            self._expect_end(env)
            return Row([Raw(f"[{env}] "), body])
        rows: list[list[Node]] = []
        cells: list[Node] = []
        while True:
            cell = self.parse_row(stop=frozenset({"&", "\\\\", "\\end", "\\cr"}))
            cells.append(cell)
            tok = self.peek()
            if tok is None:
                raise MathParseError(f"\\begin{{{env}}} without \\end")
            self.next()
            if tok == "&":
                continue
            if tok in ("\\\\", "\\cr"):
                self.skip_space()
                if self.peek() == "[":  # \\[2pt] spacing argument
                    while self.peek() not in (None, "]"):
                        self.next()
                    if self.peek() == "]":
                        self.next()
                rows.append(cells)
                cells = []
                continue
            # \end
            name = self.raw_group().strip()
            if name != env:
                raise MathParseError(f"\\begin{{{env}}} closed by \\end{{{name}}}")
            rows.append(cells)
            break
        # Drop a trailing empty row (a final "\\" before \end).
        while rows and all(_is_empty(c) for c in rows[-1]):
            rows.pop()
        return Matrix(env, rows, colspec)

    def _expect_end(self, env: str) -> None:
        if self.peek() != "\\end":
            raise MathParseError(f"\\begin{{{env}}} without \\end")
        self.next()
        name = self.raw_group().strip()
        if name != env:
            raise MathParseError(f"\\begin{{{env}}} closed by \\end{{{name}}}")


def _is_empty(node: Node) -> bool:
    return isinstance(node, Row) and all(isinstance(i, Space) for i in node.items)


def parse(src: str) -> Row:
    """Parse a TeX math string. Raises MathParseError on structurally broken input."""
    return Parser(src).parse()
