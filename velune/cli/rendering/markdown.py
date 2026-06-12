from __future__ import annotations

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown
from rich.syntax import Syntax


class CustomCodeBlock(CodeBlock):
    """A code block with syntax highlighting and line numbers for blocks >5 lines."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        line_count = len(code.splitlines())
        line_numbers = line_count > 5
        syntax = Syntax(
            code,
            self.lexer_name,
            theme="monokai",
            word_wrap=True,
            line_numbers=line_numbers,
            padding=1,
        )
        yield syntax


class CustomMarkdown(Markdown):
    """Custom Markdown renderer that overrides CodeBlock element handling."""

    elements = Markdown.elements.copy()
    elements["fence"] = CustomCodeBlock
    elements["code_block"] = CustomCodeBlock

    def __init__(self, markup: str, code_theme: str = "monokai", **kwargs) -> None:
        super().__init__(markup, code_theme=code_theme, **kwargs)


class MarkdownStreamBuffer:
    """Buffer that accumulates chunks of Markdown, handles split fences, and returns clean renderables."""

    def __init__(self) -> None:
        self._buffer = ""

    def append(self, text: str) -> None:
        self._buffer += text

    @property
    def raw_content(self) -> str:
        return self._buffer

    def get_renderable(self) -> CustomMarkdown:
        content = self._buffer

        # Clean up split code fences at the end of the buffer to prevent flicker/incorrect rendering.
        # If it ends with incomplete backticks on a new line (e.g. \n` or \n``)
        # or just starts with incomplete backticks if buffer is very short
        if content.endswith("\n`"):
            content = content[:-2]
        elif content.endswith("\n``"):
            content = content[:-3]
        elif content == "`" or content == "``":
            content = ""

        return CustomMarkdown(content)
