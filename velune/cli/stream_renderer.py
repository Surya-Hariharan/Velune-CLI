"""Streaming response renderer for the Velune REPL.

Extracted from ``VeluneREPL._handle_prompt`` to keep the main REPL file focused
on session management rather than output rendering details.

The :class:`StreamRenderer` owns the ``Live``/``console.status`` rendering loop
and returns a ``(full_content, tokens_used, interrupted)`` tuple so the REPL can
continue with memory/hook handling after the render completes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from velune._compat import uncancel_task

_log = logging.getLogger("velune.cli.stream_renderer")

if TYPE_CHECKING:
    from rich.console import Console

    from velune.cli.interrupt_controller import InterruptController


@dataclass
class RenderResult:
    """Output of a single StreamRenderer.render() call."""

    full_content: list[str] = field(default_factory=list)
    tokens_used: int = 0
    interrupted: bool = False

    @property
    def text(self) -> str:
        return "".join(self.full_content)


class StreamRenderer:
    """Renders a provider inference call as throttled streaming or blocking output.

    Args:
        console: Rich console instance shared with the REPL.
        interrupts: Interrupt controller that gates CancelledError vs user Ctrl+C.
        status_state: Mutable REPL status state for latency/tps metrics.
    """

    _MIN_UPDATE_INTERVAL = 0.08  # seconds between Live updates at high token rates

    def __init__(
        self,
        console: Console,
        interrupts: InterruptController,
        status_state: Any,
    ) -> None:
        self._console = console
        self._interrupts = interrupts
        self._status_state = status_state

    async def render(self, provider: Any, request: Any) -> RenderResult:
        """Run the provider call and render its output.

        If the provider supports streaming, renders via Rich ``Live`` with
        throttled markdown updates.  Otherwise falls back to a blocking spinner.

        Args:
            provider: A :class:`ModelProvider` (supports ``stream()`` / ``infer()``).
            request:  An :class:`InferenceRequest` to send to the provider.

        Returns:
            :class:`RenderResult` with the full text, token count, and interrupt flag.
        """
        from rich.live import Live

        from velune.cli.rendering import CustomMarkdown, MarkdownStreamBuffer, StreamStats

        result = RenderResult()

        try:
            async with self._interrupts.foreground():
                capabilities = provider.get_capabilities()
                supports_stream = getattr(capabilities, "supports_streaming", False)

                if supports_stream:
                    stream_buffer = MarkdownStreamBuffer()
                    stats = StreamStats()
                    last_update = 0.0

                    with Live(
                        "",
                        console=self._console,
                        refresh_per_second=12,
                        vertical_overflow="visible",
                    ) as live:
                        async for chunk in provider.stream(request):
                            if chunk.content:
                                stream_buffer.append(chunk.content)
                                result.full_content.append(chunk.content)
                                stats.record_chunk(chunk.content)
                                now = time.perf_counter()
                                if now - last_update >= self._MIN_UPDATE_INTERVAL:
                                    live.update(stream_buffer.get_renderable())
                                    last_update = now
                        live.update(stream_buffer.get_renderable())

                    self._status_state.last_latency_ms = stats.time_to_first_token_ms
                    self._status_state.last_tokens_per_sec = stats.tokens_per_second

                else:
                    t0 = time.perf_counter()
                    with self._console.status("[cyan]Thinking...[/cyan]"):
                        response = await provider.infer(request)
                    self._status_state.last_latency_ms = (time.perf_counter() - t0) * 1000.0
                    self._status_state.last_tokens_per_sec = None
                    result.full_content.append(response.content)
                    result.tokens_used = response.tokens_used
                    self._console.print(CustomMarkdown(response.content))

        except asyncio.CancelledError:
            if not self._interrupts.consume_user_cancelled():
                raise
            task = asyncio.current_task()
            if task is not None:
                uncancel_task(task)
            result.interrupted = True
        except KeyboardInterrupt:
            result.interrupted = True

        return result
