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

    from velune.cli.interrupts import InterruptController


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
        self._fullscreen_ui: Any | None = None
        # Provider IDs that have already gotten the "doesn't support
        # streaming" hint this session — shown once per provider, not on
        # every non-streaming turn, so it explains rather than nags.
        self._nonstream_hint_shown: set[str] = set()

    def attach_fullscreen_ui(self, ui: Any | None) -> None:
        """Route streaming updates into the fullscreen transcript when active."""
        self._fullscreen_ui = ui

    def _nonstream_hint(self, provider: Any) -> str | None:
        """First-time-only explanation for why a turn didn't stream.

        Returns None on every call after the first for a given provider, so
        callers can unconditionally check the return value instead of
        tracking their own "already shown" state.
        """
        provider_id = getattr(provider, "provider_id", None) or "This provider"
        if provider_id in self._nonstream_hint_shown:
            return None
        self._nonstream_hint_shown.add(provider_id)
        return (
            f"{provider_id} doesn't support streaming — replies arrive all at "
            "once instead of token-by-token."
        )

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
        from velune.context.cache.manager import make_cache_manager

        result = RenderResult()
        # NoOp for every provider but Anthropic, so this is a dict-lookup no-op
        # cost on the fallback path used when the tool loop is unavailable
        # (model/provider without function calling, or native_tools disabled).
        cache_manager = make_cache_manager(getattr(provider, "provider_id", ""))
        request = cache_manager.prepare(request)

        try:
            async with self._interrupts.foreground():
                capabilities = provider.get_capabilities()
                supports_stream = getattr(capabilities, "supports_streaming", False)

                if self._fullscreen_ui is not None:
                    if not supports_stream:
                        hint = self._nonstream_hint(provider)
                        if hint:
                            self._fullscreen_ui.append_system(f"ℹ {hint}")
                    t0 = time.perf_counter()
                    first_token_at: float | None = None
                    self._fullscreen_ui.begin_assistant("Thinking...")

                    if supports_stream:
                        async for chunk in provider.stream(request):
                            if chunk.content:
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                                result.full_content.append(chunk.content)
                                self._fullscreen_ui.update_assistant(result.text)
                        self._fullscreen_ui.update_assistant(result.text, final=True)
                    else:
                        response = await provider.infer(request)
                        result.full_content.append(response.content)
                        result.tokens_used = response.tokens_used
                        cache_manager.record(response.metadata)
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        self._fullscreen_ui.update_assistant(response.content, final=True)

                    self._fullscreen_ui.finish_assistant()
                    elapsed = max(time.perf_counter() - t0, 0.001)
                    if first_token_at is not None:
                        self._status_state.last_latency_ms = (first_token_at - t0) * 1000.0
                    text_len = len(result.text)
                    self._status_state.last_tokens_per_sec = (
                        (text_len / 4) / elapsed if supports_stream and text_len else None
                    )

                elif supports_stream:
                    from rich.text import Text as _Text

                    stream_buffer = MarkdownStreamBuffer()
                    stats = StreamStats()
                    last_update = 0.0

                    # Show "Thinking…" until the first token arrives so the
                    # user always has immediate visual feedback.
                    _thinking = _Text("Thinking…", style="dim")

                    with Live(
                        _thinking,
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
                    hint = self._nonstream_hint(provider)
                    if hint:
                        self._console.print(f"[dim]ℹ {hint}[/dim]")
                    t0 = time.perf_counter()
                    with self._console.status("[dim]Thinking…[/dim]"):
                        response = await provider.infer(request)
                    self._status_state.last_latency_ms = (time.perf_counter() - t0) * 1000.0
                    self._status_state.last_tokens_per_sec = None
                    result.full_content.append(response.content)
                    result.tokens_used = response.tokens_used
                    cache_manager.record(response.metadata)
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
        finally:
            # Always tear down the fullscreen assistant region — cancels the
            # "Thinking…" animation task and clears the stream marker — even
            # when generation is interrupted before the first token. Without
            # this, an early cancel leaves the animation cycling and
            # ``_stream_start`` set, corrupting the next turn's transcript.
            # ``finish_assistant`` is idempotent, so the normal path (which
            # already called it) is unaffected.
            if self._fullscreen_ui is not None:
                try:
                    self._fullscreen_ui.finish_assistant()
                except Exception:  # never let cleanup mask the real result
                    pass

        return result
