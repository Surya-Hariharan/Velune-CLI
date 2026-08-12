"""The canonical Velune colour-theme registry.

One registry, one active-theme state. Every colour the CLI paints originates
from a :class:`Theme` here and reaches call sites through the role-named
constants in :mod:`velune.cli.design` — nothing downstream should carry a raw
``#rrggbb`` literal of its own.

Why a token set rather than a single accent swap
------------------------------------------------
A theme is a coherent colour *system*: swapping only the accent leaves the
neutrals tuned for the original hue and the result reads as the default theme
wearing a hat. Each theme below therefore carries its own background, panel,
selection, and border neutrals, subtly temperature-matched to its accent (a
warm near-black under Amber, a cool one under Azure), plus its own severity
trio picked to stay distinguishable *against that background*.

Relationship to accessibility settings
--------------------------------------
A theme defines the palette. Colourblind mode and reduced motion are
independent modifiers layered on top and are *not* themes — see
``design.set_colorblind_mode`` / ``design.set_reduced_motion``. Colourblind
mode overrides whatever severity trio the active theme supplies, because the
Okabe-Ito hues exist precisely to be theme-independent.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Theme id used whenever none is configured, or a configured one is unknown.
DEFAULT_THEME_ID = "velune"


@dataclass(frozen=True, slots=True)
class Theme:
    """A complete colour system.

    Field names are *roles*, not hues: ``primary`` is "the brand accent for
    this theme", which is amber under Amber Gold. Renaming a role would ripple
    through every surface, so the roles are deliberately generic.
    """

    id: str
    name: str
    description: str

    # --- Brand / accent ---------------------------------------------------
    #: Primary accent — wordmark, prompt glyph, headings, command palette.
    primary: str
    #: Dimmer primary — secondary elements, arrows, selected-row text.
    secondary: str
    #: Companion accent, a distinct hue from ``primary``. The model palette
    #: occupies the same screen rectangle the command palette just vacated, so
    #: it needs a different hue or the swap reads as the list re-sorting itself.
    accent: str
    #: Dimmer companion accent.
    accent_soft: str

    # --- Neutrals ---------------------------------------------------------
    background: str  #: Fullscreen REPL background.
    foreground: str  #: Primary body text.
    foreground_dim: str  #: Neutral secondary text.
    muted: str  #: Dim/secondary text.
    border: str  #: Frame glyphs, separators, dividers.
    panel: str  #: Panel / palette surface background.
    selection: str  #: Selected-row background.

    # --- Semantic ---------------------------------------------------------
    success: str
    warning: str
    error: str
    info: str

    # --- Brand gradient (wordmark, progress fills) ------------------------
    gradient: tuple[str, str, str]


#: The existing Velune identity, preserved verbatim as the default. The hexes
#: below are exactly the ones ``design.py`` carried before themes existed, so
#: an untouched install renders pixel-identically to previous releases.
_VELUNE = Theme(
    id="velune",
    name="Velune",
    description="Electric indigo and cyan-blue on near-black — the default identity",
    primary="#818cf8",
    secondary="#5b63d6",
    accent="#38bdf8",
    accent_soft="#0ea5e9",
    background="#0a0a0a",
    foreground="#e8e8e6",
    foreground_dim="#a3a3a1",
    muted="#7a7a78",
    border="#4a4a48",
    panel="#131311",
    selection="#1e1e1c",
    success="#7a9b82",
    warning="#b3966e",
    error="#b3706e",
    info="#96a8ae",
    gradient=("#a78bfa", "#60a5fa", "#2dd4bf"),
)

_AMBER = Theme(
    id="amber",
    name="Amber Gold",
    description="Warm amber and soft brass on a warm near-black",
    primary="#f0b429",
    secondary="#c98f1e",
    accent="#e8894a",
    accent_soft="#c26c34",
    background="#0c0a07",
    foreground="#ece7dd",
    foreground_dim="#a8a094",
    muted="#7e766a",
    border="#4d463b",
    panel="#15110c",
    selection="#211a11",
    success="#9aa06a",
    warning="#d9a441",
    error="#c1705c",
    info="#b0a58c",
    gradient=("#f7d774", "#f0b429", "#d97706"),
)

_EMERALD = Theme(
    id="emerald",
    name="Emerald",
    description="Deep emerald and sea-green on a cool near-black",
    primary="#34d399",
    secondary="#10a37f",
    accent="#5eead4",
    accent_soft="#2dbcaa",
    background="#070b09",
    foreground="#e3ece7",
    foreground_dim="#98a89f",
    muted="#71827a",
    border="#3d4b44",
    panel="#0d1310",
    selection="#151f1a",
    success="#4ade80",
    warning="#c2a05a",
    error="#c9736c",
    info="#8fb3a6",
    gradient=("#6ee7b7", "#34d399", "#0d9488"),
)

_CRIMSON = Theme(
    id="crimson",
    name="Crimson",
    description="Restrained crimson and dusty rose on a deep neutral black",
    primary="#e5484d",
    secondary="#b8383d",
    accent="#f0918f",
    accent_soft="#c96b6b",
    background="#0b0708",
    foreground="#ede4e4",
    foreground_dim="#a89a9b",
    muted="#7f7273",
    border="#4b3e3f",
    panel="#140e0f",
    selection="#1f1618",
    success="#82a37f",
    warning="#c9a05e",
    error="#f0625f",
    info="#ab989b",
    gradient=("#fda4af", "#e5484d", "#9f1239"),
)

_AZURE = Theme(
    id="azure",
    name="Azure",
    description="Clear azure and steel blue on a cool slate black",
    primary="#3b9eff",
    secondary="#2b7fd4",
    accent="#67cfff",
    accent_soft="#3ba3c9",
    background="#070a0d",
    foreground="#e2e9f0",
    foreground_dim="#96a4b3",
    muted="#6f7d8b",
    border="#3c4652",
    panel="#0c1116",
    selection="#141c24",
    success="#6ba888",
    warning="#c4a15f",
    error="#cf7070",
    info="#8fa8bd",
    gradient=("#93c5fd", "#3b9eff", "#1d4ed8"),
)


#: Registration order — this is the order the theme palette lists them in, so
#: the default stays first and the rest follow the brief's stated sequence.
_ORDERED: tuple[Theme, ...] = (_VELUNE, _AMBER, _EMERALD, _CRIMSON, _AZURE)

THEMES: dict[str, Theme] = {theme.id: theme for theme in _ORDERED}


def list_themes() -> list[Theme]:
    """Every registered theme, in display order."""
    return list(_ORDERED)


def get(theme_id: str) -> Theme | None:
    """The theme registered under *theme_id*, or None if there isn't one."""
    return THEMES.get(str(theme_id).strip().lower())


def default_theme() -> Theme:
    """The fallback theme. Always present."""
    return THEMES[DEFAULT_THEME_ID]


def resolve(theme_id: str | None) -> tuple[Theme, bool]:
    """Resolve *theme_id* to a theme, reporting whether it was recognised.

    Returns ``(theme, ok)``. An unknown or empty id yields
    ``(default_theme(), False)`` so a corrupt or hand-edited config downgrades
    to the default instead of raising — the caller decides whether to report
    the miss, and deliberately never rewrites the config to "fix" it, since
    silently overwriting a value the user typed would destroy the evidence of
    their typo.
    """
    if not theme_id:
        return default_theme(), True
    found = get(theme_id)
    if found is None:
        return default_theme(), False
    return found, True
