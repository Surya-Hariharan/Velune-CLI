"""Reusable interactive terminal primitives, used by onboarding/setup and the
REPL alike.

High-level, one-shot helpers (``single_select``, ``multi_select``,
``text_input``, ``confirm``) run a widget standalone via ``runner.py`` and
return its result, or the ``BACK``/``CANCEL`` sentinel. Wizard chrome
(``chrome.WizardController``) constructs the same widgets directly so it can
host them inside persistent sidebar/header chrome instead.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from velune.cli.interactive.result import BACK, CANCEL, WidgetResult
from velune.cli.interactive.runner import run_standalone, run_with_status
from velune.cli.interactive.tty import is_interactive_tty
from velune.cli.interactive.widgets import (
    ConfirmWidget,
    Option,
    PickItem,
    SelectWidget,
    StatusWidget,
    TextInputWidget,
)

__all__ = [
    "BACK",
    "CANCEL",
    "WidgetResult",
    "Option",
    "PickItem",
    "StatusWidget",
    "is_interactive_tty",
    "single_select",
    "multi_select",
    "text_input",
    "confirm",
    "run_with_status",
]


async def single_select(
    title: str,
    options: Sequence[Option],
    *,
    subtitle: str = "",
    filterable: bool = False,
    initial_index: int = 0,
) -> WidgetResult:
    """Arrow-key single choice. Returns the chosen ``Option.id``, or BACK/CANCEL."""
    widget = SelectWidget(
        title=title,
        options=list(options),
        multiple=False,
        filterable=filterable,
        subtitle=subtitle,
        initial_index=initial_index,
    )
    return await run_standalone(widget)


async def multi_select(
    title: str,
    options: Sequence[Option],
    *,
    subtitle: str = "",
    initial_checked: frozenset[str] = frozenset(),
) -> WidgetResult:
    """Checkbox multi-select. Returns the checked ``Option.id`` list, or BACK/CANCEL."""
    widget = SelectWidget(
        title=title,
        options=list(options),
        multiple=True,
        filterable=False,
        subtitle=subtitle,
        initial_checked=initial_checked,
    )
    return await run_standalone(widget)


async def text_input(
    title: str,
    *,
    hint: str = "",
    password: bool = False,
    default: str = "",
    optional: bool = False,
    validate: Callable[[str], str | None] | None = None,
) -> WidgetResult:
    """Free-text field. Returns the entered string, or BACK/CANCEL."""
    widget = TextInputWidget(
        title=title,
        hint=hint,
        password=password,
        default=default,
        optional=optional,
        validate=validate,
    )
    return await run_standalone(widget)


async def confirm(
    question: str,
    *,
    hint: str = "",
    default: bool = True,
) -> WidgetResult:
    """Yes/No choice. Returns a bool, or BACK/CANCEL."""
    widget = ConfirmWidget(question=question, hint=hint, default=default)
    return await run_standalone(widget)
