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

from dataclasses import dataclass, field
from typing import Callable

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.containers import Window as PTWindow
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from velune.cli import design
from velune.cli.interactive.widget import Widget


@dataclass(kw_only=True)
class TextInputWidget(Widget):
    title: str
    hint: str = ""
    password: bool = False
    default: str = ""
    optional: bool = False
    validate: Callable[[str], str | None] | None = None

    _error: str = field(default="", init=False)
    _text_area: TextArea = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._text_area = TextArea(
            text=self.default,
            multiline=False,
            password=self.password,
            style=f"fg:{design.WHITE}",
            accept_handler=self._on_accept,
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
        lines: StyleAndTextTuples = [(f"bold fg:{design.WHITE}", f"  {self.title}\n")]
        if self.hint:
            lines.append((f"fg:{design.MUTED}", f"  {self.hint}\n"))
        if self._error:
            lines.append((f"fg:{design.DANGER}", f"  {self._error}\n"))
        lines.append(("", "\n"))
        return lines

    @property
    def container(self) -> HSplit:
        return HSplit(
            [
                PTWindow(FormattedTextControl(self._header), dont_extend_height=True),
                self._text_area,
            ]
        )

    def footer_hint(self) -> str:
        skip = "  ·  Enter to skip" if self.optional else ""
        return f"Enter confirm{skip}  ·  Esc back"
