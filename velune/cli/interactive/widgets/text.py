"""Free-text input widget — the one exception to the render()/key_bindings()
widget contract.

Line editing (cursor motion, word-backspace, selection) needs a genuine
``prompt_toolkit`` ``Buffer``, which a bare ``FormattedTextControl`` cannot
provide, so this wraps ``prompt_toolkit.widgets.TextArea`` directly and
exposes a ``.container`` for hosts to embed. Used for exactly the four text
fields the spec calls out: API keys (``password=True``), custom endpoint
URLs, optional custom model names, optional project paths — every other
onboarding/setup screen uses a menu instead.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import AnyContainer, HSplit
from prompt_toolkit.layout.containers import Window as PTWindow
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from velune.cli import design
from velune.cli.interactive import panel
from velune.cli.interactive.widget import Widget


@dataclass(kw_only=True)
class TextInputWidget(Widget):
    title: str
    hint: str = ""
    password: bool = False
    default: str = ""
    optional: bool = False
    validate: Callable[[str], str | None] | None = None
    # Draw inside the command-palette frame. Set for steps that sit between
    # palette-styled pickers (API key entry, above all) so a single flow does
    # not switch chrome halfway through.
    panelled: bool = False
    # Frame caption when panelled; defaults to the title.
    frame_title: str = ""

    _error: str = field(default="", init=False)
    _text_area: TextArea = field(init=False, repr=False)

    def __post_init__(self) -> None:
        extra: dict = {}
        if self.panelled:
            # Pin the field to one line and mark it with a prompt caret.
            # Without the explicit height the TextArea absorbs the frame's
            # spare vertical space and reads as a blank box.
            extra = {"height": 1, "prompt": [(panel.CODE, "  ❯ ")]}
        self._text_area = TextArea(
            text=self.default,
            multiline=False,
            password=self.password,
            style=(
                f"{panel.PANEL_STYLE} fg:{design.WHITE}" if self.panelled else f"fg:{design.WHITE}"
            ),
            accept_handler=self._on_accept,
            **extra,
        )

    def _on_accept(self, buf) -> bool:
        text = buf.text.strip()
        if text or not self.optional:
            if self.validate is not None:
                err = self.validate(text)
                if err:
                    self._error = err
                    return True
        self._error = ""
        self.on_submit(text)
        return True

    def _header(self) -> StyleAndTextTuples:
        if self.panelled:
            # The frame caption already carries the title — repeating it as a
            # heading inside the frame just says the same thing twice.
            lines: StyleAndTextTuples = []
            if self.hint:
                lines.append((panel.MUTED, f"  {self.hint}\n"))
            if self._error:
                lines.append((f"{panel.PANEL_STYLE} fg:{design.DANGER}", f"  {self._error}\n"))
            lines.append((panel.PANEL_STYLE, "\n"))
            return lines

        lines = [(f"bold fg:{design.WHITE}", f"  {self.title}\n")]
        if self.hint:
            lines.append((f"fg:{design.MUTED}", f"  {self.hint}\n"))
        if self._error:
            lines.append((f"fg:{design.DANGER}", f"  {self._error}\n"))
        lines.append(("", "\n"))
        return lines

    def _footer(self) -> StyleAndTextTuples:
        return [(panel.MUTED, f"\n  {self.footer_hint()}")]

    @property
    def shows_own_hints(self) -> bool:
        return self.panelled

    @property
    def container(self) -> AnyContainer:
        body = HSplit(
            [
                PTWindow(
                    FormattedTextControl(self._header),
                    dont_extend_height=True,
                    style=panel.PANEL_STYLE if self.panelled else "",
                ),
                self._text_area,
            ]
        )
        if not self.panelled:
            return body
        return panel.framed(
            HSplit(
                [
                    body,
                    PTWindow(
                        FormattedTextControl(self._footer),
                        dont_extend_height=True,
                        style=panel.PANEL_STYLE,
                    ),
                ]
            ),
            self.frame_title or self.title,
        )

    def footer_hint(self) -> str:
        skip = "  ·  Enter to skip" if self.optional else ""
        return f"Enter confirm{skip}  ·  Esc back"
