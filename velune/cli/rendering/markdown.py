from __future__ import annotations

import time

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
