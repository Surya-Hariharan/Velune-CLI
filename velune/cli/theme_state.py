"""Persistence and startup restoration for the active colour theme.

The theme id lives in the ordinary Velune configuration (``[theme] active`` in
``velune.toml``) and is read back through the ordinary
:class:`~velune.kernel.config.VeluneConfig` tree. There is no second
configuration system, no module-level "current theme" variable that outlives
the process, and no separate theme file.

Why the theme wasn't persisting before
--------------------------------------
It never was persisted. The only theme-looking setting was the ``Appearance ▸
Theme`` row in the ``/settings`` TUI, which wrote ``[appearance] theme = ...``
to ``velune.toml``. ``VeluneConfig`` has no ``appearance`` section and is
declared ``extra="ignore"``, so the value was discarded the moment the file was
parsed, and nothing anywhere read it back. The apparent "reverts to default on
restart" was a setting that had never applied in the first place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from velune.cli import themes

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.theme_state")

#: Config section and key the theme id is stored under.
THEME_SECTION = "theme"
THEME_KEY = "active"


def configured_theme_id(config: Any) -> str | None:
    """The theme id recorded in *config*, or None if there isn't one.

    Tolerates a config object without a ``theme`` section (an older
    ``velune.toml`` parsed by a newer build, or a stub in tests) rather than
    raising — the caller falls back to the default theme.
    """
    section = getattr(config, THEME_SECTION, None)
    if section is None:
        return None
    value = getattr(section, THEME_KEY, None)
    return str(value) if value else None


def resolve_startup_theme(config: Any) -> tuple[themes.Theme, str | None]:
    """Resolve the theme to paint the UI with at startup.

    Returns ``(theme, problem)``. *problem* is None on success, or a short
    human-readable description when the configured id was not recognised — in
    which case *theme* is the default. The configuration is deliberately left
    untouched in that case (see :func:`velune.cli.themes.resolve`), so the bad
    value survives for ``velune doctor`` to report and for the user to correct
    themselves.
    """
    configured = configured_theme_id(config)
    theme, ok = themes.resolve(configured)
    if not ok:
        return theme, (
            f"Unknown theme {configured!r} in [theme] active — falling back to {theme.id!r}."
        )
    return theme, None


def apply_startup_theme(config: Any) -> str | None:
    """Apply the configured theme to the design tokens. Returns any problem.

    Called during runtime construction, *before* any Console or prompt_toolkit
    style is built, so the very first frame is already painted in the chosen
    theme. That ordering is what prevents the default-theme → selected-theme
    flash the brief asks to avoid: nothing is ever rendered under the wrong
    palette, rather than being rendered and then corrected.
    """
    from velune.cli import design

    theme, problem = resolve_startup_theme(config)
    design.apply_theme(theme)
    if problem:
        _log.debug("%s", problem)
    return problem


def persist_theme(repl: VeluneREPL, theme_id: str) -> None:
    """Write *theme_id* to ``[theme] active`` and update the in-memory config.

    Reuses the existing settings-persistence helpers rather than opening its
    own writer, so the theme is saved through the same atomic path (and to the
    same file) as every other persisted setting.
    """
    from velune.cli.handlers.settings import _update_runtime_config, save_setting_to_toml

    save_setting_to_toml(repl, THEME_SECTION, THEME_KEY, theme_id)
    _update_runtime_config(repl, THEME_SECTION, THEME_KEY, theme_id)
