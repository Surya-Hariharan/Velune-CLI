"""Symbol data for the TeX-subset renderer: glyph + ASCII alternative per command.

This is a lookup table consulted by the parser (``tex.py``), not a text
replacement pass: a command becomes a glyph only once the parser has
identified it as a symbol in a math context. Extend by adding rows.
"""

from __future__ import annotations

# kind: "ord" (letter-like), "bin" (binary operator: spaced), "rel" (relation:
# spaced), "open"/"close" (delimiters), "punct".
# name: (unicode, ascii, kind)
_TABLE: dict[str, tuple[str, str, str]] = {}


def _add(kind: str, rows: dict[str, tuple[str, str]]) -> None:
    for name, (uni, asc) in rows.items():
        _TABLE[name] = (uni, asc, kind)


_add(
    "ord",
    {
        # Greek lowercase
        "alpha": ("α", "alpha"),
        "beta": ("β", "beta"),
        "gamma": ("γ", "gamma"),
        "delta": ("δ", "delta"),
        "epsilon": ("ϵ", "eps"),
        "varepsilon": ("ε", "eps"),
        "zeta": ("ζ", "zeta"),
        "eta": ("η", "eta"),
        "theta": ("θ", "theta"),
        "vartheta": ("ϑ", "theta"),
        "iota": ("ι", "iota"),
        "kappa": ("κ", "kappa"),
        "lambda": ("λ", "lambda"),
        "mu": ("μ", "mu"),
        "nu": ("ν", "nu"),
        "xi": ("ξ", "xi"),
        "pi": ("π", "pi"),
        "varpi": ("ϖ", "pi"),
        "rho": ("ρ", "rho"),
        "varrho": ("ϱ", "rho"),
        "sigma": ("σ", "sigma"),
        "varsigma": ("ς", "sigma"),
        "tau": ("τ", "tau"),
        "upsilon": ("υ", "upsilon"),
        "phi": ("ϕ", "phi"),
        "varphi": ("φ", "phi"),
        "chi": ("χ", "chi"),
        "psi": ("ψ", "psi"),
        "omega": ("ω", "omega"),
        # Greek uppercase
        "Gamma": ("Γ", "Gamma"),
        "Delta": ("Δ", "Delta"),
        "Theta": ("Θ", "Theta"),
        "Lambda": ("Λ", "Lambda"),
        "Xi": ("Ξ", "Xi"),
        "Pi": ("Π", "Pi"),
        "Sigma": ("Σ", "Sigma"),
        "Upsilon": ("Υ", "Upsilon"),
        "Phi": ("Φ", "Phi"),
        "Psi": ("Ψ", "Psi"),
        "Omega": ("Ω", "Omega"),
        # Misc ordinary
        "infty": ("∞", "inf"),
        "partial": ("∂", "d"),
        "nabla": ("∇", "nabla"),
        "forall": ("∀", "for all"),
        "exists": ("∃", "exists"),
        "nexists": ("∄", "not exists"),
        "emptyset": ("∅", "{}"),
        "varnothing": ("∅", "{}"),
        "hbar": ("ℏ", "hbar"),
        "ell": ("ℓ", "l"),
        "Re": ("ℜ", "Re"),
        "Im": ("ℑ", "Im"),
        "aleph": ("ℵ", "aleph"),
        "angle": ("∠", "angle"),
        "triangle": ("△", "triangle"),
        "degree": ("°", "deg"),
        "prime": ("′", "'"),
        "neg": ("¬", "not "),
        "lnot": ("¬", "not "),
        "cdots": ("⋯", "..."),
        "ldots": ("…", "..."),
        "dots": ("…", "..."),
        "vdots": ("⋮", ":"),
        "ddots": ("⋱", "..."),
        "top": ("⊤", "T"),
        "bot": ("⊥", "_|_"),
        "checkmark": ("✓", "v"),
        "therefore": ("∴", "therefore"),
        "because": ("∵", "because"),
    },
)
_add(
    "bin",
    {
        "cdot": ("·", "*"),
        "times": ("×", "x"),
        "div": ("÷", "/"),
        "pm": ("±", "+/-"),
        "mp": ("∓", "-/+"),
        "ast": ("∗", "*"),
        "star": ("⋆", "*"),
        "circ": ("∘", "o"),
        "bullet": ("∙", "*"),
        "oplus": ("⊕", "(+)"),
        "ominus": ("⊖", "(-)"),
        "otimes": ("⊗", "(x)"),
        "odot": ("⊙", "(.)"),
        "cup": ("∪", "U"),
        "cap": ("∩", "n"),
        "setminus": ("∖", "\\"),
        "wedge": ("∧", "/\\"),
        "land": ("∧", "and"),
        "vee": ("∨", "\\/"),
        "lor": ("∨", "or"),
        "dagger": ("†", "+"),
    },
)
_add(
    "rel",
    {
        "le": ("≤", "<="),
        "leq": ("≤", "<="),
        "ge": ("≥", ">="),
        "geq": ("≥", ">="),
        "ne": ("≠", "!="),
        "neq": ("≠", "!="),
        "approx": ("≈", "~="),
        "sim": ("∼", "~"),
        "simeq": ("≃", "~="),
        "cong": ("≅", "~="),
        "equiv": ("≡", "=="),
        "propto": ("∝", "~"),
        "ll": ("≪", "<<"),
        "gg": ("≫", ">>"),
        "in": ("∈", "in"),
        "notin": ("∉", "not in"),
        "ni": ("∋", "contains"),
        "subset": ("⊂", "subset"),
        "subseteq": ("⊆", "subseteq"),
        "supset": ("⊃", "supset"),
        "supseteq": ("⊇", "supseteq"),
        "mid": ("∣", "|"),
        "parallel": ("∥", "||"),
        "perp": ("⊥", "_|_"),
        "to": ("→", "->"),
        "rightarrow": ("→", "->"),
        "leftarrow": ("←", "<-"),
        "gets": ("←", "<-"),
        "leftrightarrow": ("↔", "<->"),
        "Rightarrow": ("⇒", "=>"),
        "Leftarrow": ("⇐", "<="),
        "Leftrightarrow": ("⇔", "<=>"),
        "implies": ("⟹", "=>"),
        "impliedby": ("⟸", "<="),
        "iff": ("⟺", "<=>"),
        "mapsto": ("↦", "|->"),
        "uparrow": ("↑", "^"),
        "downarrow": ("↓", "v"),
        "longrightarrow": ("⟶", "-->"),
        "longleftarrow": ("⟵", "<--"),
        "longleftrightarrow": ("⟷", "<-->"),
        "Longrightarrow": ("⟹", "==>"),
        "Longleftarrow": ("⟸", "<=="),
        "Longleftrightarrow": ("⟺", "<==>"),
        "longmapsto": ("⟼", "|-->"),
        "hookrightarrow": ("↪", "->"),
        "hookleftarrow": ("↩", "<-"),
        "rightleftharpoons": ("⇌", "<=>"),
        "nearrow": ("↗", "/"),
        "searrow": ("↘", "\\"),
        "Uparrow": ("⇑", "^"),
        "Downarrow": ("⇓", "v"),
        "updownarrow": ("↕", "^v"),
        "leadsto": ("⇝", "~>"),
        "nleq": ("≰", "!<="),
        "ngeq": ("≱", "!>="),
        "lesssim": ("≲", "<~"),
        "gtrsim": ("≳", ">~"),
        "asymp": ("≍", "~"),
        "triangleq": ("≜", "=def"),
        "nsubseteq": ("⊈", "not subseteq"),
        "sqsubseteq": ("⊑", "[="),
        "dashv": ("⊣", "-|"),
        "vdash": ("⊢", "|-"),
        "models": ("⊨", "|="),
        "coloneqq": ("≔", ":="),
        "doteq": ("≐", "=."),
        "prec": ("≺", "<"),
        "succ": ("≻", ">"),
    },
)
_add(
    "open",
    {
        "langle": ("⟨", "<"),
        "lfloor": ("⌊", "floor("),
        "lceil": ("⌈", "ceil("),
        "lbrace": ("{", "{"),
    },
)
_add(
    "close",
    {"rangle": ("⟩", ">"), "rfloor": ("⌋", ")"), "rceil": ("⌉", ")"), "rbrace": ("}", "}")},
)


def lookup(name: str) -> tuple[str, str, str] | None:
    """``(unicode, ascii, kind)`` for a symbol command, or None."""
    return _TABLE.get(name)


def symbol_names() -> frozenset[str]:
    return frozenset(_TABLE)


# Big operators: name → (unicode, ascii, limits-above-and-below in display mode)
BIG_OPS: dict[str, tuple[str, str, bool]] = {
    "sum": ("Σ", "sum", True),
    "prod": ("∏", "prod", True),
    "coprod": ("∐", "coprod", True),
    "bigcup": ("⋃", "U", True),
    "bigcap": ("⋂", "n", True),
    "bigoplus": ("⨁", "(+)", True),
    "bigotimes": ("⨂", "(x)", True),
    "int": ("∫", "integral", False),
    "iint": ("∬", "double integral", False),
    "iiint": ("∭", "triple integral", False),
    "oint": ("∮", "contour integral", False),
    "lim": ("lim", "lim", True),
    "limsup": ("lim sup", "lim sup", True),
    "liminf": ("lim inf", "lim inf", True),
    "max": ("max", "max", True),
    "min": ("min", "min", True),
    "sup": ("sup", "sup", True),
    "inf": ("inf", "inf", True),
    "argmax": ("argmax", "argmax", True),
    "argmin": ("argmin", "argmin", True),
}

# Upright function names (rendered as words, never italic letters run together).
FUNCTIONS = frozenset(
    {
        "sin",
        "cos",
        "tan",
        "cot",
        "sec",
        "csc",
        "arcsin",
        "arccos",
        "arctan",
        "sinh",
        "cosh",
        "tanh",
        "coth",
        "log",
        "ln",
        "lg",
        "exp",
        "det",
        "dim",
        "ker",
        "deg",
        "gcd",
        "lcm",
        "arg",
        "Pr",
        "hom",
        "mod",
        "bmod",
        "tr",
        "rank",
    }
)

# Accent command → combining character (applied to a single base character).
ACCENTS: dict[str, tuple[str, str]] = {
    "hat": ("̂", "hat"),
    "widehat": ("̂", "hat"),
    "bar": ("̄", "bar"),
    "overline": ("̅", "overline"),
    "vec": ("⃗", "vec"),
    "dot": ("̇", "dot"),
    "ddot": ("̈", "ddot"),
    "tilde": ("̃", "tilde"),
    "widetilde": ("̃", "tilde"),
    "underline": ("̲", "underline"),
}

# Blackboard bold letters for \mathbb.
BLACKBOARD = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "E": "𝔼", "F": "𝔽"}

SUPERSCRIPT = dict(
    zip(
        "0123456789+-=()niabcdefghjklmoprstuvwxyzABDEGHIJKLMNOPRTUVW",
        "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ",
        strict=True,
    )
)
SUBSCRIPT = dict(
    zip(
        "0123456789+-=()aehijklmnoprstuvx",
        "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
        strict=True,
    )
)
# Characters that may appear in a script and still be written with the maps above.
SUPERSCRIPT["−"] = "⁻"
SUBSCRIPT["−"] = "₋"
SUPERSCRIPT[" "] = " "
SUBSCRIPT[" "] = " "


def to_superscript(text: str) -> str | None:
    """Unicode superscript form, or None if any character has no superscript glyph."""
    out = [SUPERSCRIPT.get(ch) for ch in text]
    return None if any(c is None for c in out) else "".join(out)  # type: ignore[arg-type]


def to_subscript(text: str) -> str | None:
    out = [SUBSCRIPT.get(ch) for ch in text]
    return None if any(c is None for c in out) else "".join(out)  # type: ignore[arg-type]
