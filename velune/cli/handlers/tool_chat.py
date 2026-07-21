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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL
    from velune.core.types.inference import InferenceRequest
    from velune.core.types.model import ModelDescriptor
    from velune.execution.diff_preview import FileDiff
    from velune.orchestration.tool_loop import ToolLoopResult

_log = logging.getLogger("velune.cli.handlers.tool_chat")

# Human-readable verbs for tool cards: `● Write(velune/x.py)` instead of
# `● write_file {"file_path": ...}`. Unknown (e.g. MCP) tools show raw names.
_TOOL_VERBS: dict[str, str] = {
    "read_file": "Read",
    "read_directory": "List",
    "write_file": "Write",
    "create_file": "Create",
    "delete_file": "Delete",
    "grep_files": "Search",
    "find_files": "Find",
    "semantic_code_search": "Search",
    "symbol_search": "Search",
    "go_to_definition": "Navigate",
    "find_references": "References",
    "execute_command": "Run",
    "web_fetch": "Fetch",
    "terminal_history": "History",
    "git_status": "Git status",
    "git_diff": "Git diff",
    "git_log": "Git log",
    "git_blame": "Git blame",
    "git_branch": "Git branch",
    "git_commit": "Git commit",
    "git_checkout": "Git checkout",
    # No "git_push" entry: GitPushTool is intentionally not registered into
    # the autonomous tool loop (velune/tools/subsystems.py) — pushing to a
    # remote only happens through the explicit /push command, which has its
    # own approval flow. A label here would misleadingly imply the model can
    # call it directly.
}

_FILE_MUTATORS = {"write_file", "create_file", "delete_file"}
_SEARCH_TOOLS = {
    "grep_files",
    "find_files",
    "semantic_code_search",
    "symbol_search",
    "find_references",
}

_RESULT_STYLE = "class:conversation.tool.result"


def _relativize(target: str, workspace: Path | None) -> str:
    if workspace is None:
        return target
    try:
        return str(Path(target).resolve().relative_to(Path(workspace).resolve()))
    except Exception:
        return target


def _describe_call(name: str, arguments: Any, workspace: Path | None) -> tuple[str, str]:
    """(verb, primary argument) for a tool card title."""
    verb = _TOOL_VERBS.get(name, name)
    args = arguments if isinstance(arguments, dict) else {}
    target = ""
    for key in ("file_path", "path", "directory"):
        value = args.get(key)
        if isinstance(value, str) and value:
            target = _relativize(value, workspace)
            break
    if not target:
        for key in ("command", "pattern", "query", "url", "symbol"):
            value = args.get(key)
            if isinstance(value, str) and value:
                target = value
                break
    target = " ".join(target.split())
    if len(target) > 80:
        target = target[:79] + "…"
    return verb, target


def _summarize_result(
    name: str, data: dict[str, Any], diff: FileDiff | None, target: str
) -> list[tuple[str, str]]:
    """Fragments for the `⎿` result row under a resolved tool card."""
    result = str(data.get("result") or "")
    first = result.splitlines()[0].strip() if result.strip() else ""
    if data.get("error"):
        return [("class:conversation.tool.err", (first or "failed")[:160])]
    if name in _FILE_MUTATORS and diff is not None:
        from velune.execution.diff_preview import diff_stats

        added, removed = diff_stats(diff)
        label = target or str(diff.path)
        if diff.is_new_file:
            return [(_RESULT_STYLE, f"Created {label} with {added} lines")]
        if diff.is_deletion:
            return [(_RESULT_STYLE, f"Deleted {label}")]
        return [
            (
                _RESULT_STYLE,
                f"Updated {label} with {added} additions and {removed} removals",
            )
        ]
    if name == "read_file":
        if len(result) >= 2000:  # event payload truncates; count would lie
            return [(_RESULT_STYLE, f"Read {target}".strip())]
        return [(_RESULT_STYLE, f"Read {len(result.splitlines())} lines")]
    if name in _SEARCH_TOOLS:
        matches = len([ln for ln in result.splitlines() if ln.strip()])
        return [(_RESULT_STYLE, f"Found {matches} matches" if matches else "No matches")]
    if first:
        return [(_RESULT_STYLE, first[:160])]
    return [(_RESULT_STYLE, "Done")]


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
    # Model-level veto, only when it is backed by measurement.
    #
    # The original code called ``.get("tool_use")`` on model.capabilities, which
    # is a ModelCapabilityProfile (a Pydantic model, not a dict). That raised
    # AttributeError on every turn and was swallowed below, so the veto never
    # fired. It cannot simply be switched to getattr, though: tool_use *defaults*
    # to CapabilityLevel.NONE and most discovery paths never set it — Ollama
    # builds a bare profile and populates only coding/reasoning — so honouring a
    # bare NONE would disable the tool loop for nearly every local model.
    #
    # ModelProfiler stamps "tool_use_demoted" when it demotes a model that
    # repeatedly failed the structured-output check. That flag is the only NONE
    # that means "measured as incapable" rather than "never assessed".
    try:
        if model.metadata.get("tool_use_demoted"):
            _log.info(
                "Model %s was demoted to tool_use=NONE by benchmarking; using the plain chat path.",
                model.model_id,
            )
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
        # Carry the already-executed calls out with the interrupt. Their side
        # effects are on disk, so dropping them would leave the transcript
        # claiming the turn never happened.
        executed = runner.executed_invocations
        repl._tool_call_count += len(executed)
        return ToolLoopResult(
            content="",
            turns=0,
            invocations=executed,
            stop_reason="interrupted",
        )
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


def _auto_accept_enabled(repl: VeluneREPL) -> bool:
    """True when the user passed --yes (or auto-accept was set programmatically).

    Both sources are consulted because ``app.py`` writes the flag to two places:
    the container key ``runtime.auto_accept`` and the diff-preview module
    global. Checking only one is how the tool loop came to ignore ``--yes``.
    """
    try:
        if bool(repl.container.get("runtime.auto_accept")):
            return True
    except Exception:
        pass
    try:
        from velune.execution.diff_preview import is_auto_accept

        return is_auto_accept()
    except Exception:
        return False


def _make_approver(repl: VeluneREPL, ui: _ToolActivityUI):
    from velune.orchestration.tool_loop import READONLY_PERMISSIONS
    from velune.tools.safety import ApprovalMode, classify_command

    async def approver(name: str, arguments: dict[str, Any], permissions: set) -> bool:
        if permissions and permissions <= READONLY_PERMISSIONS:
            return True
        # BLOCK is checked before auto-accept: an explicit block is a stronger
        # statement than a blanket --yes.
        if repl._approval_mode is ApprovalMode.BLOCK:
            ui.note(f"[red]✗[/red] {name} denied (approval mode: block)")
            return False
        # --yes / auto-accept. This was previously honoured only by the diff
        # preview and confirm_destructive, so `velune --yes` still stopped to
        # ask for approval on every single tool call.
        if _auto_accept_enabled(repl):
            return True
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

    ui.pause_status()
    diff = ui.compute_mutation_diff(name, arguments) if name in _FILE_MUTATORS else None
    if diff is not None:
        # File mutations get the actual diff, not raw JSON.
        from velune.execution.diff_preview import diff_stats

        added, removed = diff_stats(diff)
        verb, target = _describe_call(name, arguments, ui._workspace)
        repl.console.print(
            Panel(
                f"[bold]{verb}[/bold] {target or diff.path}  [dim](+{added} -{removed})[/dim]",
                title="[yellow]Tool approval required[/yellow]",
                border_style="yellow",
                padding=(0, 2),
            )
        )
        if ui._fullscreen_ui is not None:
            ui._fullscreen_ui.append_fragment_lines(ui._diff_fragments(diff))
        else:
            from velune.execution.diff_preview import DiffPreview

            DiffPreview(repl.console).render_diff(diff)
        ui._diff_shown.add((name, str(diff.path)))
    else:
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
    # Routed through the shared prompt_toolkit widget, not rich.prompt.Prompt.
    # The old call did a blocking stdin read from inside the running fullscreen
    # application, which owns the terminal — two readers on one raw-mode stdin
    # split the user's keystrokes, so approvals hung or read as denials.
    from velune.cli.interactive import CANCEL, Option, is_interactive_tty, single_select

    if not is_interactive_tty():
        _log.debug("No interactive stdin; denying %s", name)
        if diff is not None:
            ui._diff_shown.discard((name, str(diff.path)))
        return False

    try:
        answer = await single_select(
            "Allow this tool call?",
            [
                Option(id="n", label="No — skip this call"),
                Option(id="y", label="Yes — allow once"),
                Option(id="a", label=f"Always — allow {name} for this session"),
            ],
            subtitle=name,
        )
    except KeyboardInterrupt:
        # A real interrupt must abort the turn, not read as a silent denial.
        raise
    except Exception as exc:
        _log.debug("Approval prompt unavailable (%s); denying %s", exc, name)
        if diff is not None:
            ui._diff_shown.discard((name, str(diff.path)))
        return False

    if answer is CANCEL or answer is None:
        if diff is not None:
            ui._diff_shown.discard((name, str(diff.path)))
        return False
    if answer == "a":
        repl._tool_session_grants.add(name)
        return True
    approved = answer == "y"
    if not approved and diff is not None:
        # Denied: the call never starts, so the shown-marker would otherwise
        # leak onto the next (approved) write to the same file.
        ui._diff_shown.discard((name, str(diff.path)))
    return approved


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
        # Tool-card state, keyed by the provider's tool-call id.
        self._cards: dict[str, Any] = {}
        self._targets: dict[str, str] = {}
        self._pending_diffs: dict[str, FileDiff] = {}
        # (tool name, resolved path) pairs whose diff already rendered at
        # approval time — consumed (one-shot) at tool_end to avoid a repeat.
        self._diff_shown: set[tuple[str, str]] = set()
        try:
            ws = repl.container.get("runtime.workspace")
            self._workspace: Path | None = Path(ws) if ws else None
        except Exception:
            self._workspace = None

    # Runner events are synchronous callbacks on the loop task.
    def on_event(self, event: str, data: dict[str, Any]) -> None:
        if event == "turn":
            self.last_turn_streamed = False
            first = data.get("turn", 1) == 1
            if self._fullscreen_ui is not None:
                # Turn 1 opens the response block (◆ Velune + cycling verbs);
                # later turns just resume the spinner under the tool cards.
                if first:
                    self._fullscreen_ui.begin_assistant()
                else:
                    self._fullscreen_ui.begin_assistant(
                        "Continuing…", cycle=False, show_label=False
                    )
            else:
                label = "Thinking…" if first else "Continuing…"
                self._start_status(f"[dim]{label}[/dim]")
        elif event == "content_delta":
            self._render_delta(data.get("text", ""))
        elif event == "turn_end":
            self._finish_live()
            self.pause_status()
        elif event == "tool_start":
            self._on_tool_start(data)
        elif event == "tool_end":
            self._on_tool_end(data)
        elif event == "tool_denied":
            self._on_tool_denied(data)

    # ── Tool cards ───────────────────────────────────────────────────

    def _on_tool_start(self, data: dict[str, Any]) -> None:
        self.any_tool_ran = True
        name = str(data.get("name") or "")
        call_id = str(data.get("id") or "")
        arguments = data.get("arguments")
        verb, target = _describe_call(name, arguments, self._workspace)
        self._targets[call_id] = target
        if name in _FILE_MUTATORS:
            diff = self.compute_mutation_diff(name, arguments)
            if diff is not None:
                self._pending_diffs[call_id] = diff
        if self._fullscreen_ui is not None:
            self._cards[call_id] = self._fullscreen_ui.add_tool_card(verb, target)
        else:
            shown = f"[dim]({target})[/dim]" if target else ""
            self._console.print(f"  [cyan]●[/cyan] [bold]{verb}[/bold]{shown}")

    def _on_tool_end(self, data: dict[str, Any]) -> None:
        name = str(data.get("name") or "")
        call_id = str(data.get("id") or "")
        error = bool(data.get("error"))
        diff = self._pending_diffs.pop(call_id, None)
        target = self._targets.pop(call_id, "")
        summary = _summarize_result(name, data, diff, target)
        shown_key = (name, str(diff.path)) if diff is not None else None
        already_shown = shown_key in self._diff_shown if shown_key else False
        if shown_key and already_shown:
            self._diff_shown.discard(shown_key)  # one-shot: next call re-renders
        show_diff = diff is not None and not error and not already_shown

        if self._fullscreen_ui is not None:
            card = self._cards.pop(call_id, None)
            if card is None and self._cards:
                # Defensive: resolve the oldest unresolved card if ids drift.
                oldest = next(iter(self._cards))
                card = self._cards.pop(oldest)
            if card is not None:
                card.resolve(summary, error=error)
            else:
                self._fullscreen_ui.append_fragment_lines([[(_RESULT_STYLE, "  ⎿ "), *summary]])
            if show_diff:
                self._fullscreen_ui.append_fragment_lines(self._diff_fragments(diff))
        else:
            mark = "[red]✗[/red]" if error else "[green]✓[/green]"
            plain = "".join(text for _style, text in summary)
            self._console.print(
                f"    {mark} [dim]{plain} · {data.get('duration_ms', 0):.0f} ms[/dim]"
            )
            if show_diff:
                from velune.execution.diff_preview import DiffPreview

                DiffPreview(self._console).render_diff(diff)

    def _on_tool_denied(self, data: dict[str, Any]) -> None:
        name = str(data.get("name") or "")
        call_id = str(data.get("id") or "")
        self._pending_diffs.pop(call_id, None)
        self._targets.pop(call_id, None)
        verb = _TOOL_VERBS.get(name, name)
        if self._fullscreen_ui is not None:
            self._fullscreen_ui.append_fragment_lines(
                [
                    [
                        ("class:conversation.tool.warn", "✗ "),
                        ("class:conversation.tool.name", verb),
                        (_RESULT_STYLE, " — denied"),
                    ]
                ]
            )
        else:
            self._console.print(f"  [yellow]✗ {verb} denied[/yellow]")

    def compute_mutation_diff(self, name: str, arguments: Any) -> FileDiff | None:
        """FileDiff for a file-mutation call, from pre-write disk state.

        Must run before the tool touches disk (tool_start / approval time).
        Any failure returns None — the card simply renders without a diff.
        """
        try:
            args = arguments if isinstance(arguments, dict) else {}
            file_path = args.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                return None
            from velune.execution.diff_preview import compute_file_diff
            from velune.execution.path_guard import resolve_in_workspace

            workspace = self._workspace or Path.cwd()
            path = resolve_in_workspace(file_path, workspace, label=name)
            proposed = args.get("content", "") if name == "write_file" else ""
            if not isinstance(proposed, str):
                return None
            return compute_file_diff(path, proposed)
        except Exception:
            return None

    def _diff_fragments(self, diff: FileDiff) -> list[list[tuple[str, str]]]:
        from velune.cli.rendering.diff_fragments import render_diff_fragments

        try:
            width = max(40, int(self._fullscreen_ui._width()))
        except Exception:
            width = 100
        return render_diff_fragments(diff, width)

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
        # Interrupt/teardown: any card still spinning resolves as interrupted
        # so the next turn starts from a settled transcript.
        for card in list(self._cards.values()):
            try:
                card.cancel("Interrupted")
            except Exception:
                pass
        self._cards.clear()
        self._pending_diffs.clear()
        self._targets.clear()
        if self._fullscreen_ui is not None:
            self._fullscreen_ui.cancel_live_cards()
