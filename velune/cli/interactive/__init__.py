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

from velune.cli.interactive import host
from velune.cli.interactive.result import BACK, CANCEL, WidgetResult
from velune.cli.interactive.runner import run_standalone
from velune.cli.interactive.runner import run_with_status as _run_with_status
from velune.cli.interactive.tty import is_interactive_tty
from velune.cli.interactive.widgets import (
    ConfirmWidget,
    Option,
    PaletteSelectWidget,
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
    palette: bool = False,
    frame_title: str = "",
) -> WidgetResult:
    """Arrow-key single choice. Returns the chosen ``Option.id``, or BACK/CANCEL.

    ``palette=True`` draws the choice as the REPL's two-column command palette
    (search column + detail column in a titled frame) instead of a plain list,
    so flows reached from a slash command keep the palette's look — and, when a
    host is installed (the REPL), draws it *in* that palette's float above the
    prompt box rather than in an Application of its own below it.
    """
    if palette and (installed := host.active()) is not None:
        return await installed.select(
            title,
            options,
            subtitle=subtitle,
            filterable=filterable,
            initial_index=initial_index,
            frame_title=frame_title,
        )

    cls = PaletteSelectWidget if palette else SelectWidget
    extra = {"frame_title": frame_title} if palette else {}
    widget = cls(
        title=title,
        options=list(options),
        multiple=False,
        filterable=filterable,
        subtitle=subtitle,
        initial_index=initial_index,
        **extra,
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
    palette: bool = False,
    frame_title: str = "",
) -> WidgetResult:
    """Free-text field. Returns the entered string, or BACK/CANCEL.

    ``palette=True`` draws the field inside the command-palette frame, for
    steps that sit between palette-styled pickers. Under an installed host (the
    REPL) it goes further: the REPL's *own* prompt box becomes the field, so a
    flow never grows a second input area halfway through.
    """
    if palette and (installed := host.active()) is not None:
        return await installed.prompt_text(
            title,
            hint=hint,
            password=password,
            optional=optional,
            validate=validate,
            frame_title=frame_title,
        )

    widget = TextInputWidget(
        title=title,
        hint=hint,
        password=password,
        default=default,
        optional=optional,
        validate=validate,
        panelled=palette,
        frame_title=frame_title,
    )
    return await run_standalone(widget)


async def run_with_status(coro, **kwargs):
    """Await *coro* behind a spinner, then show a ✓/✗ frame (see ``runner``).

    Routed through the installed host so the spinner between two steps of a
    flow appears in the same panel those steps used, instead of tearing that
    panel down to draw a one-line Application below the prompt box.

    No ``is_interactive_tty()`` gate on the host path, unlike the standalone one
    below: a host is installed only by a running REPL, which already owns an
    Application, and the host renders into that rather than to the terminal. If
    its output happens to be going nowhere the work still runs and still returns
    its value — whereas gating here would silently route a flow's middle step
    back out to a second Application.
    """
    if (installed := host.active()) is not None:
        return await installed.run_status(coro, **kwargs)
    return await _run_with_status(coro, **kwargs)


async def confirm(
    question: str,
    *,
    hint: str = "",
    default: bool = True,
) -> WidgetResult:
    """Yes/No choice. Returns a bool, or BACK/CANCEL.

    Unlike the pickers above this has no ``palette`` flag — there is only one
    way to draw a Yes/No — but it still follows an installed host, so a confirm
    reached mid-flow (``/providers`` → remove) stays in the same panel as the
    steps on either side of it.
    """
    if (installed := host.active()) is not None:
        return await installed.confirm(question, hint=hint, default=default)

    widget = ConfirmWidget(question=question, hint=hint, default=default)
    return await run_standalone(widget)
