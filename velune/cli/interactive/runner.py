"""Standalone widget runner — one private ``Application`` per call.

For interactive prompts that aren't part of the onboarding wizard chrome
(REPL palettes, ``/session``/``/project`` pickers, one-off confirmations).
Wizard stages use ``chrome.WizardController.run_widget`` instead, which
reuses a single long-lived ``Application`` across the whole run.
"""

from __future__ import annotations

from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import merge_key_bindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from velune.cli import design
from velune.cli.interactive.keys import common_bindings
from velune.cli.interactive.result import BACK, CANCEL
from velune.cli.interactive.widget import Widget
from velune.cli.interactive.widgets.text import TextInputWidget


def _footer_window(widget: Widget | TextInputWidget) -> Window:
    def _render():
        return [(f"fg:{design.FAINT}", f"\n  {widget.footer_hint()}")]

    return Window(FormattedTextControl(_render), dont_extend_height=True)


async def run_standalone(
    widget: Widget | TextInputWidget,
    *,
    input: Any | None = None,
    output: Any | None = None,
) -> Any:
    """Run *widget* full-screen-off in its own ``Application``.

    Returns whatever the widget submits, or the ``BACK``/``CANCEL`` sentinel
    if the user pressed Esc / Ctrl-C. Esc is only wired when the widget's
    ``on_back`` was left at its no-op default — callers that want "no back
    target" simply don't override it, and Esc still resolves to ``BACK`` so
    the caller can decide whether that's meaningful.

    ``input``/``output`` are forwarded to ``Application`` as-is (``None``
    keeps prompt_toolkit's own defaults) — the seam exists so tests can drive
    a real widget through synthetic key events via
    ``prompt_toolkit.input.create_pipe_input`` instead of mocking the widget.
    """
    result: dict[str, Any] = {}

    def _submit(value: Any) -> None:
        result["value"] = value
        app.exit()

    def _back() -> None:
        result["value"] = BACK
        app.exit()

    def _cancel() -> None:
        result["value"] = CANCEL
        app.exit()

    widget.on_submit = _submit
    widget.on_back = _back
    widget.on_cancel = _cancel

    is_text = isinstance(widget, TextInputWidget)

    if is_text:
        body = widget.container
        kb = common_bindings(on_cancel=_cancel, on_back=_back)
    else:
        body = Window(FormattedTextControl(widget.render, focusable=True))
        kb = merge_key_bindings(
            [widget.key_bindings(), common_bindings(on_cancel=_cancel, on_back=_back)]
        )

    layout = Layout(HSplit([body, _footer_window(widget)]))

    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        mouse_support=True,
        input=input,
        output=output,
    )
    await app.run_async()
    return result.get("value", CANCEL)
