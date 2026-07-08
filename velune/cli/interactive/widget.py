"""Widget protocol: one interactive primitive, host-agnostic.

A ``Widget`` never touches ``Application`` and never decides what "submit"
means — that decision belongs to the host. ``run_standalone`` (runner.py)
wires the callbacks to ``app.exit()``; ``WizardController`` (chrome.py) wires
them to "advance wizard state and re-render the body region" without ever
tearing down the Application. The same widget instance works in both places.

Subclasses must NOT bind ``escape`` or ``c-c`` in ``key_bindings()`` — those
belong exclusively to ``keys.common_bindings()`` so eager-binding precedence
stays unambiguous (see keys.py docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings

R = TypeVar("R")


@dataclass(kw_only=True)
class Widget(Generic[R]):
    """Base shape for every interactive primitive (select, confirm, ...).

    ``TextInputWidget`` is the one widget that does not use ``render()`` /
    ``key_bindings()`` — it embeds a real ``prompt_toolkit.widgets.TextArea``
    instead (see widgets/text.py) because free-text editing needs genuine
    cursor motion. Hosts special-case ``isinstance(widget, TextInputWidget)``.

    All fields are keyword-only (``kw_only=True``) so subclasses can add their
    own required fields (title, options, ...) without Python dataclass field-
    ordering restrictions.

    ``on_submit``/``on_back``/``on_cancel`` default to no-ops: stage-authoring
    code constructs a widget with just its business fields (title, options,
    ...) and the host (``runner.run_standalone`` or
    ``chrome.WizardController.run_widget``) overwrites these three attributes
    to wire the widget's outcome to its own await/Future plumbing. Widgets
    never need to know which host they're running under.
    """

    on_submit: Callable[[R], None] = lambda value: None  # noqa: E731
    on_back: Callable[[], None] = lambda: None  # noqa: E731
    on_cancel: Callable[[], None] = lambda: None  # noqa: E731

    def render(self) -> StyleAndTextTuples:
        raise NotImplementedError

    def key_bindings(self) -> KeyBindings:
        raise NotImplementedError

    def footer_hint(self) -> str:
        """Key-hint strip text, e.g. '↑↓ navigate  ·  Enter select  ·  Esc back'."""
        raise NotImplementedError
