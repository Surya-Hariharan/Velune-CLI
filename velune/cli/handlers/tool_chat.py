"""Native tool-calling glue for the REPL chat path.

Runs :class:`velune.orchestration.tool_loop.ToolLoopRunner` for a chat turn,
with the REPL's interactive approval UX and live tool-activity rendering.
Returns ``None`` whenever the tool path should not (or cannot) run, so the
caller falls back to the legacy streaming render — chat never breaks because
tools are unavailable.

Approval policy (per call):

- read-only scopes (``filesystem.read``, ``git.read``) — always auto-approved.
- ``execute_command`` — classified by :func:`velune.tools.safety.classify_command`:
  BLOCK verdicts are denied outright; SAFE verdicts auto-run when the session
  approval mode is ``safe``; everything else prompts.
- ``/approve block`` — denies every non-read-only call without prompting.
- everything else (writes, git mutations, network, MCP) — prompts, with a
  per-session "always allow this tool" option.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL
    from velune.core.types.inference import InferenceRequest
    from velune.core.types.model import ModelDescriptor
    from velune.orchestration.tool_loop import ToolLoopResult

_log = logging.getLogger("velune.cli.handlers.tool_chat")


def tool_loop_available(repl: VeluneREPL, model: ModelDescriptor, provider: Any) -> bool:
    """Cheap gate: should this chat turn attempt the native tool loop?"""
    config = getattr(repl.runtime, "config", None)
    execution = getattr(config, "execution", None)
    if execution is not None and not getattr(execution, "native_tools", True):
        return False
    if model.model_id in repl._tools_unsupported_models:
        return False
    try:
        caps = provider.get_capabilities()
        if not getattr(caps, "supports_function_calling", False):
            return False
    except Exception:
        return False
    # Model-level signal, when present. Absence is not a veto — the
    # first-turn fallback handles providers that reject tool payloads.
    try:
        from velune.core.types.model import CapabilityLevel

        level = (model.capabilities or {}).get("tool_use")
        if level == CapabilityLevel.NONE:
            return False
    except Exception:
        pass
    # Tool registry is a Tier-1 (background) subsystem; before it's warm,
    # chat proceeds on the legacy path.
    try:
        return repl.container.get("runtime.tool_registry") is not None
    except Exception:
        return False


async def run_tool_chat(
    repl: VeluneREPL,
    model: ModelDescriptor,
    provider: Any,
    request: InferenceRequest,
) -> ToolLoopResult | None:
    """Run the chat turn through the tool loop; None means "use legacy path".

    A provider/model that rejects the tool payload on the *first* turn (HTTP
    400 from servers without tool support) is remembered for the session and
    the caller silently falls back. Errors after tools have already executed
    are surfaced — silently rerunning the prompt could repeat side effects.
    """
    from velune._compat import uncancel_task
    from velune.core.errors.provider import InferenceError
    from velune.orchestration.tool_loop import ToolLoopResult, ToolLoopRunner
    from velune.tools.base.tool import ToolCallContext

    if not tool_loop_available(repl, model, provider):
        return None

    registry = repl.container.get("runtime.tool_registry")
    workspace = repl.container.get("runtime.workspace")
    config = getattr(repl.runtime, "config", None)
    max_turns = getattr(getattr(config, "execution", None), "max_tool_turns", 10)

    from pathlib import Path

    ctx = ToolCallContext(
        run_id=repl._session_id,
        actor="repl",
        workspace=Path(workspace) if workspace else None,
        hook_dispatcher=repl._hook_dispatcher,
        session_id=repl._episodic_session_id or repl._session_id,
    )

    ui = _ToolActivityUI(repl)
    runner = ToolLoopRunner(
        provider,
        registry,
        mcp_registry=repl._mcp_registry,
        approver=_make_approver(repl, ui),
        ctx=ctx,
        max_turns=max_turns,
        on_event=ui.on_event,
    )

    try:
        async with repl._interrupts.foreground():
            result = await runner.run(request)
    except asyncio.CancelledError:
        if not repl._interrupts.consume_user_cancelled():
            raise
        task = asyncio.current_task()
        if task is not None:
            uncancel_task(task)
        ui.close()
        return ToolLoopResult(content="", turns=0, stop_reason="interrupted")
    except InferenceError as exc:
        ui.close()
        if ui.any_tool_ran:
            # Tools already had side effects; do not silently replay the turn.
            raise
        repl._tools_unsupported_models.add(model.model_id)
        _log.info(
            "Provider rejected tool payload for %s; falling back to plain chat: %s",
            model.model_id,
            exc,
        )
        return None
    finally:
        ui.close()

    repl._tool_call_count += len(result.invocations)
    # Final answer: already on screen if the last turn streamed; otherwise
    # (non-streaming provider) render it now. The caller never prints.
    if result.content.strip() and not ui.last_turn_streamed:
        from velune.cli.rendering import CustomMarkdown

        repl.console.print(CustomMarkdown(result.content))
    return result


# ── Approval UX ─────────────────────────────────────────────────────────────


def _make_approver(repl: VeluneREPL, ui: _ToolActivityUI):
    from velune.orchestration.tool_loop import READONLY_PERMISSIONS
    from velune.tools.safety import ApprovalMode, classify_command

    async def approver(name: str, arguments: dict[str, Any], permissions: set) -> bool:
        if permissions and permissions <= READONLY_PERMISSIONS:
            return True
        if repl._approval_mode is ApprovalMode.BLOCK:
            ui.note(f"[red]✗[/red] {name} denied (approval mode: block)")
            return False
        if name == "execute_command" and isinstance(arguments.get("command"), str):
            verdict = classify_command(arguments["command"])
            if verdict.mode is ApprovalMode.BLOCK:
                ui.note(f"[red]✗[/red] {name} blocked: {verdict.reason}")
                return False
            if verdict.mode is ApprovalMode.SAFE and repl._approval_mode is ApprovalMode.SAFE:
                return True
        if name in repl._tool_session_grants:
            return True
        return await _prompt_approval(repl, ui, name, arguments)

    return approver


async def _prompt_approval(
    repl: VeluneREPL, ui: _ToolActivityUI, name: str, arguments: dict[str, Any]
) -> bool:
    """Interactive y/n/a prompt. Fails closed when no interactive stdin."""
    import json

    from rich.panel import Panel
    from rich.prompt import Prompt

    ui.pause_status()
    args_preview = json.dumps(arguments, default=str, ensure_ascii=False)
    if len(args_preview) > 400:
        args_preview = args_preview[:400] + "…"
    repl.console.print(
        Panel(
            f"[bold]{name}[/bold]\n[dim]{args_preview}[/dim]",
            title="[yellow]Tool approval required[/yellow]",
            border_style="yellow",
            padding=(0, 2),
        )
    )
    try:
        answer = await asyncio.to_thread(
            Prompt.ask,
            "  Allow? [bold]y[/bold]es / [bold]n[/bold]o / [bold]a[/bold]lways this session",
            choices=["y", "n", "a"],
            default="n",
            console=repl.console,
        )
    except (EOFError, KeyboardInterrupt, Exception) as exc:
        _log.debug("Approval prompt unavailable (%s); denying %s", exc, name)
        return False
    if answer == "a":
        repl._tool_session_grants.add(name)
        return True
    return answer == "y"


# ── Live activity rendering ─────────────────────────────────────────────────


class _ToolActivityUI:
    """Console feedback for loop events.

    Model turns show a spinner until the first streamed token arrives, then a
    live-updating markdown render (same throttling approach as
    :class:`velune.cli.stream_renderer.StreamRenderer`). Tool calls render as
    one activity line each. ``last_turn_streamed`` tells the caller whether
    the final answer was already rendered live (so it must not print again).
    """

    _MIN_UPDATE_INTERVAL = 0.08  # seconds between Live refreshes

    def __init__(self, repl: VeluneREPL) -> None:
        self._console = repl.console
        # When a fullscreen UI owns the terminal, `Live`/`console.status()`
        # render nothing visible at all (force_interactive=False suppresses
        # their redraw output) — route streaming/status through the
        # fullscreen UI's own begin_assistant/update_assistant/finish_assistant
        # instead, which is also how it gets the same markdown+syntax-
        # highlighted rendering as regular chat streaming, for free.
        self._fullscreen_ui = getattr(repl, "_fullscreen_ui", None)
        self._stream_text = ""
        self._status: Any | None = None
        self._live: Any | None = None
        self._stream_buffer: Any | None = None
        self._last_live_update = 0.0
        self.any_tool_ran = False
        self.last_turn_streamed = False

    # Runner events are synchronous callbacks on the loop task.
    def on_event(self, event: str, data: dict[str, Any]) -> None:
        if event == "turn":
            self.last_turn_streamed = False
            label = "Thinking…" if data.get("turn", 1) == 1 else "Continuing…"
            if self._fullscreen_ui is not None:
                self._fullscreen_ui.begin_assistant(label)
            else:
                self._start_status(f"[dim]{label}[/dim]")
        elif event == "content_delta":
            self._render_delta(data.get("text", ""))
        elif event == "turn_end":
            self._finish_live()
            self.pause_status()
        elif event == "tool_start":
            self.any_tool_ran = True
            args = str(data.get("arguments", ""))
            if len(args) > 120:
                args = args[:120] + "…"
            self._console.print(
                f"  [cyan]●[/cyan] [bold]{data.get('name')}[/bold] [dim]{args}[/dim]"
            )
        elif event == "tool_end":
            mark = "[red]✗[/red]" if data.get("error") else "[green]✓[/green]"
            self._console.print(f"    {mark} [dim]{data.get('duration_ms', 0):.0f} ms[/dim]")

    def note(self, markup: str) -> None:
        self.pause_status()
        self._console.print(f"  {markup}")

    # ── Streaming text ───────────────────────────────────────────────

    def _render_delta(self, text: str) -> None:
        if not text:
            return
        self.last_turn_streamed = True

        if self._fullscreen_ui is not None:
            self._stream_text += text
            self._fullscreen_ui.update_assistant(self._stream_text)
            return

        import time as _time

        if self._live is None:
            self.pause_status()
            try:
                from rich.live import Live

                from velune.cli.rendering import MarkdownStreamBuffer

                self._stream_buffer = MarkdownStreamBuffer()
                self._live = Live(
                    "",
                    console=self._console,
                    refresh_per_second=12,
                    vertical_overflow="visible",
                )
                self._live.start()
            except Exception:
                self._live = None
                self._stream_buffer = None
        if self._stream_buffer is None or self._live is None:
            return
        self._stream_buffer.append(text)
        now = _time.perf_counter()
        if now - self._last_live_update >= self._MIN_UPDATE_INTERVAL:
            try:
                self._live.update(self._stream_buffer.get_renderable())
            except Exception:
                pass
            self._last_live_update = now

    def _finish_live(self) -> None:
        if self._fullscreen_ui is not None:
            if self._stream_text:
                self._fullscreen_ui.update_assistant(self._stream_text, final=True)
            self._fullscreen_ui.finish_assistant()
            self._stream_text = ""
            return
        if self._live is not None:
            try:
                if self._stream_buffer is not None:
                    self._live.update(self._stream_buffer.get_renderable())
                self._live.stop()
            except Exception:
                pass
        self._live = None
        self._stream_buffer = None

    # ── Spinner ──────────────────────────────────────────────────────

    def _start_status(self, label: str) -> None:
        self.pause_status()
        try:
            self._status = self._console.status(label)
            self._status.start()
        except Exception:
            self._status = None

    def pause_status(self) -> None:
        if self._status is not None:
            try:
                self._status.stop()
            except Exception:
                pass
            self._status = None

    def close(self) -> None:
        self._finish_live()
        self.pause_status()
