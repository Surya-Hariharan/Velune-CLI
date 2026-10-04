from __future__ import annotations

import time

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown, TableElement
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from velune.cli import design

# Glyphs that mark a fenced block as a chart/diagram rather than code: wrapping
# such a block at the terminal width destroys its alignment, so it is cropped.
_CHART_GLYPHS = frozenset("█▇▆▅▄▃▂▁▏▎▍▌▋▊▉░▒▓┌┐└┘├┤┬┴┼─│╭╮╯╰═║")
_CHART_LEXERS = frozenset({"ascii", "chart", "diagram", "graph"})


def _looks_like_chart(code: str, lexer: str) -> bool:
    if lexer.lower() in _CHART_LEXERS:
        return True
    return sum(ch in _CHART_GLYPHS for ch in code) >= 3


class CustomCodeBlock(CodeBlock):
    """Code block with syntax highlighting; mermaid flowcharts and ASCII charts get special care."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        lexer = (self.lexer_name or "").lower()

        if lexer == "mermaid":
            from velune.cli.rendering.mermaid import render_mermaid

            tree = render_mermaid(code, options.max_width - 2)
            if tree is not None:
                yield Text(" ")
                yield tree
                yield Text(" ")
                return

        if _looks_like_chart(code, lexer):
            yield Text(" ")
            for raw in code.splitlines():
                yield Text(" " + raw, no_wrap=True, overflow="ellipsis")
            yield Text(" ")
            return

        line_count = len(code.splitlines())
        yield Syntax(
            code,
            self.lexer_name,
            theme="monokai",
            word_wrap=True,
            line_numbers=line_count > 5,
            padding=1,
        )


class CustomTableElement(TableElement):
    """Markdown table with visible borders whose long cells wrap instead of truncating."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        table = Table(
            box=box.ROUNDED,
            border_style=design.FAINT,
            header_style=f"bold {design.ACCENT}",
            show_lines=False,
            pad_edge=True,
            expand=False,
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                table.add_column(
                    column.content.copy(),
                    overflow="fold",
                    no_wrap=False,
                    justify=column.justify if column.justify != "default" else "left",
                )
        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*[element.content for element in row.cells])
        yield table


class CustomMarkdown(Markdown):
    """Markdown renderer with terminal-friendly code blocks, tables and diagrams."""

    elements = Markdown.elements.copy()
    elements["fence"] = CustomCodeBlock
    elements["code_block"] = CustomCodeBlock
    elements["table_open"] = CustomTableElement

    def __init__(self, markup: str, code_theme: str = "monokai", **kwargs) -> None:
        super().__init__(markup, code_theme=code_theme, **kwargs)


class MarkdownStreamBuffer:
    """Buffer that accumulates streamed Markdown and returns flicker-free renderables.

    Two stabilization passes run before rendering:

    1. Trailing partial fences (a lone ` or `` at the end of the buffer) are
       trimmed so they never flash as literal backticks.
    2. An *open* code fence is virtually closed, so code blocks syntax-highlight
       progressively while tokens stream instead of rendering as broken text
       until the closing fence arrives.

    Rendering is cached per buffer state — repeated get_renderable() calls
    between appends (e.g. from Live refreshes) cost nothing.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._cached: CustomMarkdown | None = None

    def append(self, text: str) -> None:
        self._buffer += text
        self._cached = None

    @property
    def raw_content(self) -> str:
        return self._buffer

    @staticmethod
    def _stabilize(content: str) -> str:
        # Trim a partial fence forming at the very end of the buffer.
        for tail in ("\n``", "\n`"):
            if content.endswith(tail):
                content = content[: -len(tail)]
                break
        if content in ("`", "``"):
            return ""

        # Count fence openings/closings; an odd count means a code block is
        # still streaming — close it virtually for stable highlighting.
        fence_count = 0
        for line in content.splitlines():
            if line.lstrip().startswith("```"):
                fence_count += 1
        if fence_count % 2 == 1:
            if not content.endswith("\n"):
                content += "\n"
            content += "```"
        return content

    def get_renderable(self) -> CustomMarkdown:
        if self._cached is None:
            self._cached = CustomMarkdown(self._stabilize(self._buffer))
        return self._cached


class StreamStats:
    """Tracks throughput of a single streamed response for the status bar."""

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._first_token_at: float | None = None
        self._chars = 0

    def record_chunk(self, text: str) -> None:
        if self._first_token_at is None:
            self._first_token_at = time.perf_counter()
        self._chars += len(text)

    @property
    def time_to_first_token_ms(self) -> float | None:
        if self._first_token_at is None:
            return None
        return (self._first_token_at - self._start) * 1000.0

    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self._start

    @property
    def tokens_per_second(self) -> float:
        # ~4 chars per token is the standard rough estimate.
        elapsed = self.elapsed_s
        if elapsed <= 0:
            return 0.0
        return (self._chars / 4) / elapsed

    @property
    def approx_tokens(self) -> int:
        return self._chars // 4
