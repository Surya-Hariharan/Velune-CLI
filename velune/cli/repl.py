"""VeluneREPL — prompt_toolkit-based interactive REPL (lifecycle & routing only).

All ``_cmd_*`` handler implementations have been extracted into focused modules
under ``velune/cli/handlers/``.  This file owns:
  - State initialisation (``__init__``)
  - prompt_toolkit session setup (``_build_prompt_session``)
  - Main event loop (``run``)
  - Lifecycle helpers (``_shutdown_repl``, ``_archive_current_session``, …)
  - Slash command dispatch (``_handle_slash_command``)
  - Thin ``_cmd_*`` delegation stubs (2-3 lines each)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

_log = logging.getLogger("velune.cli.repl")

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText

from velune._compat import uncancel_task
from velune.cli.slash_commands import SlashCommandRegistry
from velune.core.runtime import RuntimeContext
from velune.core.types.model import ModelDescriptor

if TYPE_CHECKING:
    from velune.providers.base import ModelProvider


class VeluneREPL:
    def __init__(self, runtime: RuntimeContext) -> None:
        self.runtime = runtime
        self.container = runtime.container
        self.console = runtime.console
        self.active_model: ModelDescriptor | None = None
        self._cognition_job_id: str | None = None
        from velune.cli.modes import ModeManager
        from velune.cli.statusbar import StatusBarState

        try:
            self._runtime_profile = self.container.get("runtime.profile")
        except Exception:
            self._runtime_profile = None
        self._mode_manager = ModeManager(runtime_profile=self._runtime_profile)
        self._status_state = StatusBarState(
            profile_label=self._runtime_profile.label if self._runtime_profile else None,
        )
        self._completer = None
        self._command_palette = None
        self.session_tokens: int = 0
        self.session_cost: float = 0.0
        self._history_file = Path.home() / ".velune" / "repl_history"
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        self._conversation: list[dict] = []
        self._hunk_review_mode: bool = False
        from velune.cli.interrupts import InterruptController
        from velune.cli.sessions import SessionStore
        from velune.cli.workspaces import WorkspaceRegistry

        self._interrupts = InterruptController()
        from velune.cli.stream_renderer import StreamRenderer

        self._stream_renderer = StreamRenderer(
            console=self.console,
            interrupts=self._interrupts,
            status_state=self._status_state,
        )
        self._session_store = SessionStore()
        self._workspace_registry = WorkspaceRegistry()
        self._exit_requested = False
        import time as _time

        self._session_start_time: float = _time.monotonic()
        self._tool_call_count: int = 0
        from velune.tools.safety import ApprovalMode

        self._approval_mode: ApprovalMode = ApprovalMode.ASK
        from velune.orchestration.role_assignments import CouncilRoleMap

        self._assignments_path = Path.home() / ".velune" / "council_roles.json"
        self._role_map = CouncilRoleMap.load(self._assignments_path)
        self._project_profile = self._load_project_profile()
        self._registry = self._build_registry()
        from velune.cli.handlers.councilmodel import apply_role_overrides_to_orchestrator

        apply_role_overrides_to_orchestrator(self)
        self._episodic_session_id: str | None = None
        self._cached_branch: str | None = None
        self._branch_last_checked: float = 0.0
        from velune.context.utilization import ContextUtilizationTracker
        from velune.hooks import HookDispatcher

        self._context_tracker = ContextUtilizationTracker()
        workspace_path = self.container.get("runtime.workspace")
        self._hook_dispatcher = HookDispatcher(
            workspace=Path(workspace_path) if workspace_path else None,
            session_id=None,
        )
        from velune.mcp.registry import MCPServerRegistry

        self._mcp_registry = MCPServerRegistry(
            workspace=Path(workspace_path) if workspace_path else None,
        )
        from velune.plugins.manager import PluginManager

        self._plugin_manager = PluginManager(
            workspace=Path(workspace_path) if workspace_path else None,
        )
        try:
            self._job_registry = self.container.get("runtime.job_registry")
        except Exception:
            self._job_registry = None
        try:
            self._alert_store = self.container.get("runtime.alert_store")
        except Exception:
            self._alert_store = None
        self._prev_ctx_pct: float = 0.0
        self._fullscreen_ui = None

    # ------------------------------------------------------------------
    # Workspace trust
    # ------------------------------------------------------------------

    def _ensure_workspace_trust(self) -> bool:
        from velune.core import trust

        workspace = self._mcp_registry.workspace
        if trust.is_trusted(workspace):
            return True

        has_project_config = (workspace / ".mcp.json").exists() or (
            workspace / "velune.toml"
        ).exists()
        if not has_project_config:
            return False

        from rich.prompt import Confirm

        self.console.print(
            f"[yellow]This workspace ({workspace}) is not trusted.[/yellow]\n"
            "[dim]It contains project-level MCP / provider config that can run "
            "local commands and redirect API traffic.[/dim]"
        )
        try:
            decision = Confirm.ask("  Trust this directory?", default=False)
        except Exception:
            decision = False

        if decision:
            trust.trust(workspace)
            self.console.print(
                "[dim]Workspace trusted. Use [bold]/trust forget[/bold] to revoke.[/dim]"
            )
            return True

        self.console.print(
            "[dim]Continuing untrusted — project MCP servers and base_url "
            "overrides are disabled.[/dim]"
        )
        return False

    # ------------------------------------------------------------------
    # prompt_toolkit session
    # ------------------------------------------------------------------

    def _build_prompt_session(self) -> PromptSession:
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        from velune.cli import design
        from velune.cli.autocomplete import CommandEntry, SlashCompleter
        from velune.cli.command_palette import PALETTE_STYLES, CommandPalette
        from velune.cli.statusbar import STATUS_BAR_STYLES
        from velune.cli.validators import InlineSyntaxValidator

        style = Style.from_dict(
            {
                "prompt.frame": design.FAINT,
                "prompt.prefix": f"{design.ACCENT} bold",
                "prompt.branch": design.MUTED,
                "prompt.model": design.FAINT,
                "prompt.mode": design.HIGHLIGHT,
                "prompt.arrow": f"{design.ACCENT_SOFT} bold",
                "ctx.ok": f"{design.OK} bold",
                "ctx.warn": f"{design.WARN} bold",
                "ctx.danger": f"{design.DANGER} bold",
                "mode.godly": f"{design.ENERGY} bold",
                "mode.optimus": f"{design.WARN} bold",
                **STATUS_BAR_STYLES,
                **PALETTE_STYLES,
            }
        )

        try:
            models = self.container.get("runtime.model_registry").list_all()
            model_ids = [m.model_id for m in models]
        except Exception:
            model_ids = []

        entries = [
            CommandEntry(
                name=cmd.name,
                description=cmd.description,
                category=cmd.category,
                aliases=tuple(cmd.aliases),
            )
            for cmd in self._registry.all_unique()
            if not cmd.hidden
        ]
        completer = SlashCompleter(
            commands=entries,
            model_ids=model_ids,
            show_command_completions=False,
        )
        self._completer = completer

        palette = CommandPalette(self._registry.all_unique())
        self._command_palette = palette

        kb = KeyBindings()

        @kb.add("c-c")
        def _(event):
            buffer = event.app.current_buffer
            if buffer.text:
                buffer.text = ""
                buffer.cursor_position = 0
                self._interrupts.reset_exit_window()
            elif self._interrupts.note_interrupt():
                event.app.exit(exception=KeyboardInterrupt)
            else:
                event.app.invalidate()

        @kb.add("escape", "enter")
        def _insert_newline_meta(event):
            event.app.current_buffer.insert_text("\n")

        @kb.add("c-j")
        def _insert_newline_ctrl_j(event):
            event.app.current_buffer.insert_text("\n")

        palette.add_bindings(kb)

        session = PromptSession(
            history=FileHistory(str(self._history_file)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=True,
            complete_in_thread=True,
            validator=InlineSyntaxValidator(),
            validate_while_typing=True,
            style=style,
            mouse_support=False,
            wrap_lines=True,
            key_bindings=kb,
            bottom_toolbar=self._render_toolbar,
            reserve_space_for_menu=19,
        )
        palette.attach(session)
        return session

    def _build_fullscreen_ui(self):
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        from velune.cli import design
        from velune.cli.autocomplete import CommandEntry, SlashCompleter
        from velune.cli.command_palette import PALETTE_STYLES, CommandPalette
        from velune.cli.fullscreen import FullscreenREPLUI
        from velune.cli.statusbar import STATUS_BAR_STYLES
        from velune.cli.validators import InlineSyntaxValidator

        style_fragments = {
            "prompt.frame": design.FAINT,
            "prompt.prefix": f"{design.ACCENT} bold",
            "prompt.branch": design.MUTED,
            "prompt.model": design.FAINT,
            "prompt.mode": design.HIGHLIGHT,
            "prompt.arrow": f"{design.ACCENT_SOFT} bold",
            "ctx.ok": f"{design.OK} bold",
            "ctx.warn": f"{design.WARN} bold",
            "ctx.danger": f"{design.DANGER} bold",
            "mode.godly": f"{design.ENERGY} bold",
            "mode.optimus": f"{design.WARN} bold",
            **STATUS_BAR_STYLES,
            **PALETTE_STYLES,
        }

        try:
            models = self.container.get("runtime.model_registry").list_all()
            model_ids = [m.model_id for m in models]
        except Exception:
            model_ids = []

        entries = [
            CommandEntry(
                name=cmd.name,
                description=cmd.description,
                category=cmd.category,
                aliases=tuple(cmd.aliases),
            )
            for cmd in self._registry.all_unique()
            if not cmd.hidden
        ]
        completer = SlashCompleter(
            commands=entries,
            model_ids=model_ids,
            show_command_completions=False,
        )
        self._completer = completer

        palette = CommandPalette(self._registry.all_unique())
        self._command_palette = palette

        kb = KeyBindings()
        palette.add_bindings(kb)

        def _interrupt(event):
            if self._interrupts.note_interrupt():
                return True
            event.app.invalidate()
            return False

        return FullscreenREPLUI(
            status_state=self._status_state,
            history=FileHistory(str(self._history_file)),
            completer=completer,
            validator=InlineSyntaxValidator(),
            style_fragments=style_fragments,
            key_bindings=kb,
            on_interrupt=_interrupt,
            on_status_render=self._refresh_status_state,
        )

    def _render_toolbar(self):
        from velune.cli.statusbar import render_status_bar

        self._refresh_status_state()
        return render_status_bar(self._status_state)

    def _refresh_status_state(self) -> None:
        workspace_path = self.container.get("runtime.workspace")
        folder_name = Path(workspace_path).name if workspace_path else "velune"

        if self.active_model:
            self._context_tracker.max_tokens = self.active_model.context_length
            self._context_tracker.update(self._conversation)

        self._status_state.exit_hint = self._interrupts.exit_hint_active
        self._status_state.model_id = self.active_model.model_id if self.active_model else None
        self._status_state.mode_label = self._mode_manager.current.value.upper()
        self._status_state.context_pct = (
            self._context_tracker.percentage if self.active_model else 0.0
        )
        if self.active_model:
            self._status_state.context_used = self._context_tracker.used_tokens
            self._status_state.context_max = self._context_tracker.max_tokens
        else:
            self._status_state.context_used = None
            self._status_state.context_max = None
        self._status_state.session_cost = self.session_cost
        self._status_state.workspace_name = folder_name
        if self._job_registry is not None:
            self._status_state.bg_job_count = self._job_registry.active_count()

    def _get_prompt_tokens(self) -> FormattedText:
        from velune.cli import design
        from velune.cli.modes import SessionMode

        workspace_path = self.container.get("runtime.workspace")
        if workspace_path:
            workspace_dir = Path(workspace_path)
            folder_name = workspace_dir.name
            now = time.monotonic()
            if self._cached_branch is None or (now - self._branch_last_checked) > 5.0:
                from velune.repository.tracker import GitTracker

                try:
                    self._cached_branch = GitTracker(workspace_dir).get_active_branch()
                except Exception:
                    self._cached_branch = "unknown"
                self._branch_last_checked = now
            active_branch = self._cached_branch
        else:
            folder_name = "velune"
            active_branch = "non-git"

        tokens: list[tuple[str, str]] = [
            ("class:prompt.frame", "╭ "),
            ("class:prompt.prefix", folder_name),
        ]

        if active_branch and active_branch not in ("non-git", "unknown"):
            tokens.append(("class:prompt.branch", f" ({active_branch})"))

        if not self._mode_manager.is_normal():
            label = self._mode_manager.current.value.upper()
            if self._mode_manager.current == SessionMode.GODLY:
                tokens.append(("class:mode.godly", f" [{label}]"))
            elif self._mode_manager.current == SessionMode.OPTIMUS:
                tokens.append(("class:mode.optimus", f" [{label}]"))
            else:
                tokens.append(("class:prompt.mode", f" [{label}]"))

        if self.active_model:
            tokens.append(("class:prompt.model", f" · {self.active_model.model_id}"))

            self._context_tracker.max_tokens = self.active_model.context_length
            self._context_tracker.update(self._conversation)

            pct = self._context_tracker.percentage

            if pct < design.CTX_WARN_PCT:
                bar_style = "class:ctx.ok"
            elif pct < design.CTX_DANGER_PCT:
                bar_style = "class:ctx.warn"
            else:
                bar_style = "class:ctx.danger"
            tokens.append((bar_style, f" · {pct:.0f}%"))

        tokens.append(("class:prompt.frame", "\n╰"))
        tokens.append(("class:prompt.arrow", "❯ "))

        self._refresh_status_state()

        pct = self._status_state.context_pct
        prev_pct = self._prev_ctx_pct
        for threshold in (70, 90):
            if prev_pct < threshold <= pct:
                try:
                    from velune.events import Event

                    bus = self.container.get("runtime.bus")
                    asyncio.ensure_future(
                        bus.emit(
                            Event(
                                event_type="context.threshold_crossed",
                                source="repl",
                                data={"pct": pct, "threshold": threshold},
                            )
                        )
                    )
                except Exception:
                    pass
        self._prev_ctx_pct = pct

        return FormattedText(tokens)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        from velune.cli.handlers.model import restore_active_model
        from velune.cli.handlers.plugins import register_plugin_commands

        ui = self._build_fullscreen_ui()
        self._fullscreen_ui = ui
        self.console = ui.console
        self.runtime.console = ui.console
        self.container.register_instance("runtime.console", ui.console)
        self._stream_renderer.attach_fullscreen_ui(ui)
        ui_task = asyncio.create_task(ui.run(), name="velune.fullscreen_ui")
        ui_task.add_done_callback(lambda _task: ui.request_exit())

        restore_active_model(self)
        await self._start_episodic_session()

        model_id = self.active_model.model_id if self.active_model else ""
        _hook_start = await self._hook_dispatcher.dispatch_session_start(
            session_id=self._episodic_session_id or self._hook_dispatcher.session_id,
            model_id=model_id,
        )
        if _hook_start.system_message:
            _log.debug("Session start hook message: %s", _hook_start.system_message)

        try:
            trusted = self._ensure_workspace_trust()
            self._mcp_registry.load_config(trusted=trusted)
            if self._mcp_registry._entries:
                server_count = len(self._mcp_registry._entries)
                results = await self._mcp_registry.connect_all()
                ok = sum(1 for v in results.values() if v)
                if ok:
                    _log.debug("MCP: %s/%s server(s) connected", ok, server_count)
        except Exception as exc:
            _log.debug("MCP auto-connect error (non-fatal): %s", exc)

        try:
            new_plugins = self._plugin_manager.load()
            if new_plugins:
                register_plugin_commands(self, new_plugins)
                self._plugin_manager.wire_hooks(self._hook_dispatcher)
                self._plugin_manager.wire_mcp(self._mcp_registry)
        except Exception as exc:
            _log.debug("Plugin load error (non-fatal): %s", exc)

        self._interrupts.install()
        try:
            self._workspace_registry.touch(Path(self.container.get("runtime.workspace")))
        except Exception:
            pass

        try:
            while not self._exit_requested:
                try:
                    raw = await ui.read_input()
                    from velune.cli.handlers.council import poll_and_render_alerts

                    poll_and_render_alerts(self)
                    text = raw.strip()
                    if text:
                        self._interrupts.reset_exit_window()
                    if not text:
                        continue
                    if text.lower() in ("clear", "cls"):
                        await self._cmd_clear("")
                    elif text.startswith("/"):
                        await self._handle_slash_command(text)
                    else:
                        await self._handle_prompt(text)
                except KeyboardInterrupt:
                    self.console.print()
                    break
                except EOFError:
                    self.console.print()
                    break
                except SystemExit:
                    break
                except asyncio.CancelledError:
                    if not self._interrupts.consume_user_cancelled():
                        raise
                    task = asyncio.current_task()
                    if task is not None:
                        uncancel_task(task)
                    self._print_interrupted_frame()
                except Exception as e:
                    from velune.cli.rendering.error_panel import (
                        render_error,
                        render_unexpected_error,
                    )
                    from velune.core.errors.catalog import VeluneError

                    if isinstance(e, VeluneError):
                        self.console.print(render_error(e))
                    else:
                        self.console.print(render_unexpected_error(e))
        finally:
            self._interrupts.uninstall()
            try:
                await self._shutdown_repl()
            finally:
                ui.stop()
                try:
                    await ui_task
                except Exception:
                    pass

    def _print_interrupted_frame(self) -> None:
        self.console.print("[dim]╭─[/dim] [yellow]Generation interrupted[/yellow]")
        self.console.print("[dim]╰─[/dim] [dim]Ready for next command[/dim]")

    async def _shutdown_repl(self) -> None:
        try:
            import json as _json
            import tempfile

            transcript_path = ""
            if self._conversation:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, encoding="utf-8"
                ) as tf:
                    _json.dump(self._conversation, tf)
                    transcript_path = tf.name

            _hook_stop = await self._hook_dispatcher.dispatch_stop(
                reason="normal_exit",
                transcript_path=transcript_path,
                session_id=self._episodic_session_id or self._hook_dispatcher.session_id,
            )
            if _hook_stop.blocked:
                if _hook_stop.block_reason:
                    self.console.print(
                        f"[yellow]Stop blocked by hook:[/yellow] {_hook_stop.block_reason}"
                    )
                if _hook_stop.additional_context:
                    self._conversation.append(
                        {"role": "assistant", "content": _hook_stop.additional_context}
                    )
            elif _hook_stop.additional_context:
                self._conversation.append(
                    {"role": "assistant", "content": _hook_stop.additional_context}
                )
        except Exception as exc:
            _log.debug("Stop hook error (non-fatal): %s", exc)

        self.console.print("[dim]Saving session...[/dim]")
        try:
            self._archive_current_session()
        except Exception as exc:
            _log.warning("Session archive on exit failed: %s", exc)
        await self._end_episodic_session()

        try:
            await self._mcp_registry.disconnect_all()
        except Exception as exc:
            _log.debug("MCP disconnect error (non-fatal): %s", exc)

        self.console.print("[dim]Stopping background tasks...[/dim]")
        try:
            task_registry = self.container.get("runtime.task_registry")
            await task_registry.cancel_all(timeout=5.0)
        except Exception as exc:
            _log.warning("Background task cancellation failed: %s", exc)
        self.console.print("[dim]Goodbye.[/dim]")

    def _archive_current_session(self) -> None:
        has_user = any(m.get("role") == "user" for m in self._conversation)
        has_assistant = any(m.get("role") == "assistant" for m in self._conversation)
        if not (has_user and has_assistant):
            return
        workspace = str(self.container.get("runtime.workspace") or "")
        self._session_store.save(
            self._conversation,
            workspace=workspace,
            model_id=self.active_model.model_id if self.active_model else "unknown",
            mode=self._mode_manager.current.value,
            total_tokens=self.session_tokens,
        )

    def _print_startup_banner(self) -> None:
        from velune import __version__
        from velune.cli.banner import render_startup_banner
        from velune.providers.keystore import list_configured_providers

        hardware = self.container.get("runtime.hardware")
        configured = list_configured_providers()
        ollama_live = "ollama" in configured
        workspace = self.container.get("runtime.workspace")
        workspace_path = str(Path(workspace).resolve()) if workspace else "unknown"
        model_id = self.active_model.model_id if self.active_model else None

        pt_name = None
        if self._project_profile:
            if isinstance(self._project_profile, dict):
                pt_name = self._project_profile.get("display_name")
            else:
                pt_name = getattr(self._project_profile, "display_name", None)

        render_startup_banner(
            console=self.console,
            hardware_profile=hardware,
            configured_providers=configured,
            ollama_live=ollama_live,
            workspace_path=workspace_path,
            active_model_id=model_id,
            version=__version__,
            project_type_name=pt_name,
            runtime_profile_label=self._runtime_profile.label if self._runtime_profile else None,
        )

    # ------------------------------------------------------------------
    # Slash command dispatch
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, text: str) -> None:
        parts = text[1:].split(None, 1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self._registry.get(cmd_name)
        if cmd is None:
            self.console.print(
                f"[red]Unknown command: /{cmd_name}[/red]  "
                f"[dim]Type /help to see all commands.[/dim]"
            )
            return

        if self._completer is not None:
            self._completer.record_use(cmd.name)

        try:
            await cmd.handler(args)
        except SystemExit:
            raise
        except Exception as e:
            from velune.cli.rendering.error_panel import render_error, render_unexpected_error
            from velune.core.errors.catalog import VeluneError

            if isinstance(e, VeluneError):
                self.console.print(render_error(e))
            else:
                self.console.print(render_unexpected_error(e))

    def _build_registry(self) -> SlashCommandRegistry:
        from velune.cli.slash_dispatcher import build_slash_registry

        return build_slash_registry(self)

    # ------------------------------------------------------------------
    # Core helpers (used by handlers and prompt loop)
    # ------------------------------------------------------------------

    def _require(self, key: str, label: str):
        """Return container[key] or print an error and return None."""
        try:
            val = self.container.get(key)
            if val is None:
                raise ValueError
            return val
        except Exception:
            self.console.print(
                f"[red]{label.capitalize()} is unavailable.[/red] "
                f"[dim]→ Run [bold]/doctor[/bold] to diagnose.[/dim]"
            )
            return None

    async def _resolve_active_model_and_provider(
        self,
    ) -> tuple[ModelDescriptor | None, ModelProvider | None]:
        if self.active_model:
            provider_registry = self.container.get("runtime.provider_registry")
            provider = provider_registry.get(self.active_model.provider_id)
            return self.active_model, provider

        model_registry = self.container.get("runtime.model_registry")
        models = model_registry.list_all()
        if not models:
            return None, None

        provider_registry = self.container.get("runtime.provider_registry")
        for model in models:
            provider = provider_registry.get(model.provider_id)
            if provider:
                self.active_model = model
                return model, provider
        return None, None

    def _load_project_profile(self):
        workspace = self.container.get("runtime.workspace")
        if not workspace:
            return None
        profile_path = Path(workspace) / ".velune" / "project_profile.json"
        if profile_path.exists():
            try:
                import json

                return json.loads(profile_path.read_text())
            except Exception:
                pass
        try:
            from velune.repository.project_type import ProjectTypeDetector

            return ProjectTypeDetector().detect(Path(workspace))
        except Exception:
            return None

    def _display_usage(self, model: ModelDescriptor, tokens: int) -> None:
        self.session_tokens += tokens
        cost_per_token = (model.cost_per_1k_tokens or 0.0) / 1000
        query_cost = tokens * cost_per_token
        self.session_cost += query_cost

        parts = [f"[dim]{tokens:,} tokens"]
        if query_cost > 0:
            parts.append(f"~${query_cost:.4f}")
        parts.append(f"session: {self.session_tokens:,} tokens")
        if self.session_cost > 0:
            parts.append(f"~${self.session_cost:.4f}[/dim]")
        else:
            parts.append("[/dim]")

        self.console.print(" · ".join(parts))

    async def _emit_turn_events(
        self, user_text: str, response_text: str, model_id: str, tokens: int
    ) -> None:
        try:
            from velune.events import Event

            bus = self.container.get("runtime.bus")
            await bus.emit(
                Event(
                    event_type="turn.completed",
                    source="repl",
                    data={
                        "user": user_text[:200],
                        "response": response_text[:200],
                        "model_id": model_id,
                        "tokens": tokens,
                    },
                )
            )
        except Exception:
            pass

    async def _retrieve_semantic_context(self, text: str) -> str | None:
        try:
            import asyncio as _asyncio

            retrieval = self.container.get("runtime.retrieval")
            if not retrieval:
                return None
            results = await _asyncio.wait_for(retrieval.search(text, limit=3), timeout=2.0)
            if not results:
                return None
            snippets = "\n\n".join(r.content for r in results if r.content)
            return f"[Relevant past context]\n{snippets}" if snippets else None
        except Exception:
            return None

    async def _start_episodic_session(self) -> None:
        try:
            episodic = self.container.get("runtime.episodic_session_memory")
            workspace = str(self.container.get("runtime.workspace") or "")
            model_id = self.active_model.model_id if self.active_model else "unknown"
            self._episodic_session_id = await episodic.start_session(
                workspace=workspace, model_id=model_id
            )
        except Exception as exc:
            _log.debug("Could not start episodic session: %s", exc)

    async def _end_episodic_session(self) -> None:
        if not self._episodic_session_id:
            return
        try:
            episodic = self.container.get("runtime.episodic_session_memory")
            await episodic.end_session(self._episodic_session_id, self._conversation)
        except Exception as exc:
            _log.debug("Could not end episodic session: %s", exc)
        finally:
            self._episodic_session_id = None

    async def _handle_prompt(self, text: str) -> None:
        """Process a freeform (non-slash-command) user prompt."""
        from velune.core.types.inference import InferenceRequest

        model, provider = await self._resolve_active_model_and_provider()
        if model is None or provider is None:
            from velune.cli.rendering.error_panel import render_error
            from velune.core.errors.catalog import NoModelsAvailableError

            self.console.print(render_error(NoModelsAvailableError()))
            self.console.print(
                "[dim]→ Run [bold]/model discover[/bold] or [bold]/providers[/bold] "
                "to configure a model.[/dim]"
            )
            return

        # PrePrompt hook — may inject context or transform the user message
        try:
            from velune.context.mention_resolver import MentionResolver

            resolver = MentionResolver(
                workspace=Path(self.container.get("runtime.workspace") or "."),
                conversation=self._conversation,
            )
            resolved_text, mentioned_files = await resolver.resolve(text)
            if resolved_text != text:
                text = resolved_text

            _hook_pre = await self._hook_dispatcher.dispatch_pre_prompt(
                user_message=text,
                session_id=self._episodic_session_id or self._hook_dispatcher.session_id,
                mentioned_files=[str(mf.resolved_path) for mf in mentioned_files],
            )
            if _hook_pre.transformed_message:
                text = _hook_pre.transformed_message
            if _hook_pre.additional_context:
                self._conversation.append(
                    {"role": "system", "content": _hook_pre.additional_context}
                )
            if _hook_pre.blocked:
                if _hook_pre.block_reason:
                    self.console.print(
                        f"[yellow]Prompt blocked by hook:[/yellow] {_hook_pre.block_reason}"
                    )
                return

            # Auto-lint any .py files mentioned
            try:
                from velune.analysis.linter import PythonLinter, render_lint_panel

                linter = PythonLinter()
                for mf in mentioned_files:
                    if mf.resolved_path.suffix == ".py":
                        diags = await asyncio.to_thread(linter.lint_file, mf.resolved_path)
                        errors = [d for d in diags if d.severity == "error"]
                        if errors:
                            render_lint_panel(self.console, mf.resolved_path.name, errors)
            except Exception as exc:
                _log.debug("Auto-lint error (non-fatal): %s", exc)

        except Exception as exc:
            _log.debug("Mention resolution error (non-fatal): %s", exc)

        self._conversation.append({"role": "user", "content": text})

        mode_config = self._mode_manager.config

        if mode_config.context_compression and self._conversation:
            from velune.context.extractive import compress_conversation

            self._conversation = compress_conversation(
                self._conversation,
                max_tokens=mode_config.max_context_tokens,
            )

        retrieved_context = await self._retrieve_semantic_context(text)

        base_messages = self._conversation[-50:]
        if retrieved_context:
            effective_messages = base_messages[:-1] + [
                {"role": "system", "content": retrieved_context},
                base_messages[-1],
            ]
        else:
            effective_messages = base_messages

        request = InferenceRequest(
            model_id=model.model_id,
            messages=effective_messages,
            temperature=mode_config.temperature,
            max_tokens=4096,
        )

        render = await self._stream_renderer.render(provider, request)
        full_content = render.full_content
        tokens_used = render.tokens_used
        interrupted = render.interrupted

        if interrupted:
            self.console.print()
            self._print_interrupted_frame()
            partial = "".join(full_content)
            if partial.strip():
                self._conversation.append(
                    {"role": "assistant", "content": partial + "\n\n[response interrupted]"}
                )
            else:
                if self._conversation and self._conversation[-1].get("role") == "user":
                    self._conversation.pop()
            return

        assistant_text = "".join(full_content)

        try:
            _hook_msg = await self._hook_dispatcher.dispatch_message_display(
                message=assistant_text,
                role="assistant",
                session_id=self._episodic_session_id or self._hook_dispatcher.session_id,
            )
            if _hook_msg.transformed_message:
                assistant_text = _hook_msg.transformed_message
            if _hook_msg.system_message:
                self.console.print(f"[dim]{_hook_msg.system_message}[/dim]")
        except Exception as exc:
            _log.debug("MessageDisplay hook error (non-fatal): %s", exc)

        self._conversation.append({"role": "assistant", "content": assistant_text})
        effective_tokens = tokens_used or len(assistant_text) // 4
        self._display_usage(model, effective_tokens)
        from velune.core.task_registry import track

        track(
            asyncio.create_task(
                self._emit_turn_events(text, assistant_text, model.model_id, effective_tokens),
                name="emit_turn_events",
            )
        )

    # ------------------------------------------------------------------
    # Thin _cmd_* delegation stubs — all logic lives in handlers/
    # ------------------------------------------------------------------

    async def _cmd_help(self, args: str) -> None:
        from velune.cli.handlers.session import cmd_help

        await cmd_help(self, args)

    async def _cmd_exit(self, args: str) -> None:
        from velune.cli.handlers.session import cmd_exit

        await cmd_exit(self, args)

    async def _cmd_clear(self, args: str) -> None:
        from velune.cli.handlers.session import cmd_clear

        await cmd_clear(self, args)

    async def _cmd_new(self, args: str) -> None:
        from velune.cli.handlers.session import cmd_new

        await cmd_new(self, args)

    async def _cmd_history(self, args: str) -> None:
        from velune.cli.handlers.session import cmd_history

        await cmd_history(self, args)

    async def _cmd_stats(self, args: str) -> None:
        from velune.cli.handlers.session import cmd_stats

        await cmd_stats(self, args)

    async def _cmd_settings(self, args: str) -> None:
        from velune.cli.handlers.settings import cmd_settings

        await cmd_settings(self, args)

    async def _cmd_config(self, args: str) -> None:
        from velune.cli.handlers.settings import cmd_config

        await cmd_config(self, args)

    async def _cmd_hooks(self, args: str) -> None:
        from velune.cli.handlers.settings import cmd_hooks

        await cmd_hooks(self, args)

    async def _cmd_approve(self, args: str) -> None:
        from velune.cli.handlers.settings import cmd_approve

        await cmd_approve(self, args)

    async def _cmd_doctor(self, args: str) -> None:
        from velune.cli.handlers.settings import cmd_doctor

        await cmd_doctor(self, args)

    async def _cmd_sandbox(self, args: str) -> None:
        from velune.cli.handlers.settings import cmd_sandbox

        await cmd_sandbox(self, args)

    async def _cmd_model(self, args: str) -> None:
        from velune.cli.handlers.model import cmd_model

        await cmd_model(self, args)

    async def _cmd_models(self, args: str) -> None:
        from velune.cli.handlers.model import cmd_models

        await cmd_models(self, args)

    async def _cmd_pull(self, args: str) -> None:
        from velune.cli.handlers.model import cmd_pull

        await cmd_pull(self, args)

    async def _cmd_delete(self, args: str) -> None:
        from velune.cli.handlers.model import cmd_delete

        await cmd_delete(self, args)

    async def _cmd_bench(self, args: str) -> None:
        from velune.cli.handlers.model import cmd_bench

        await cmd_bench(self, args)

    async def _cmd_cognition(self, args: str) -> None:
        from velune.cli.handlers.cognition import cmd_cognition

        await cmd_cognition(self, args)

    async def _cmd_run(self, args: str) -> None:
        from velune.cli.handlers.council import cmd_run

        await cmd_run(self, args)

    async def _cmd_council(self, args: str) -> None:
        from velune.cli.handlers.council import cmd_council

        await cmd_council(self, args)

    async def _cmd_jobs(self, args: str) -> None:
        from velune.cli.handlers.council import cmd_jobs

        await cmd_jobs(self, args)

    async def _cmd_dashboard(self, args: str) -> None:
        from velune.cli.handlers.council import cmd_dashboard

        await cmd_dashboard(self, args)

    async def _cmd_session(self, args: str) -> None:
        from velune.cli.handlers.session_mgmt import cmd_session

        await cmd_session(self, args)

    async def _cmd_project(self, args: str) -> None:
        from velune.cli.handlers.workspace import cmd_project

        await cmd_project(self, args)

    async def _cmd_memory(self, args: str) -> None:
        from velune.cli.handlers.memory import cmd_memory

        await cmd_memory(self, args)

    async def _cmd_context(self, args: str) -> None:
        from velune.cli.handlers.memory import cmd_context

        await cmd_context(self, args)

    async def _cmd_graph(self, args: str) -> None:
        from velune.cli.handlers.memory import cmd_graph

        await cmd_graph(self, args)

    async def _cmd_optimus(self, args: str) -> None:
        from velune.cli.handlers.mode import cmd_optimus

        await cmd_optimus(self, args)

    async def _cmd_godly(self, args: str) -> None:
        from velune.cli.handlers.mode import cmd_godly

        await cmd_godly(self, args)

    async def _cmd_normal(self, args: str) -> None:
        from velune.cli.handlers.mode import cmd_normal

        await cmd_normal(self, args)

    async def _cmd_mode(self, args: str) -> None:
        from velune.cli.handlers.mode import cmd_mode

        await cmd_mode(self, args)

    async def _cmd_councilmodel(self, args: str) -> None:
        from velune.cli.handlers.councilmodel import cmd_councilmodel

        await cmd_councilmodel(self, args)

    async def _cmd_councilmodel_show(self, args: str) -> None:
        from velune.cli.handlers.councilmodel import cmd_councilmodel_show

        await cmd_councilmodel_show(self)

    async def _cmd_diff(self, args: str) -> None:
        from velune.cli.handlers.git import cmd_diff

        await cmd_diff(self, args)

    async def _cmd_undo(self, args: str) -> None:
        from velune.cli.handlers.git import cmd_undo

        await cmd_undo(self, args)

    async def _cmd_hunk(self, args: str) -> None:
        from velune.cli.handlers.git import cmd_hunk

        await cmd_hunk(self, args)

    async def _cmd_push(self, args: str) -> None:
        from velune.cli.handlers.git import cmd_push

        await cmd_push(self, args)

    async def _cmd_pr(self, args: str) -> None:
        from velune.cli.handlers.git import cmd_pr

        await cmd_pr(self, args)

    async def _cmd_issue(self, args: str) -> None:
        from velune.cli.handlers.git import cmd_issue

        await cmd_issue(self, args)

    async def _cmd_mcp(self, args: str) -> None:
        from velune.cli.handlers.mcp import cmd_mcp

        await cmd_mcp(self, args)

    async def _cmd_plugin(self, args: str) -> None:
        from velune.cli.handlers.plugins import cmd_plugin

        await cmd_plugin(self, args)

    async def _cmd_lint(self, args: str) -> None:
        from velune.cli.handlers.code_intel import cmd_lint

        await cmd_lint(self, args)

    async def _cmd_refactor(self, args: str) -> None:
        from velune.cli.handlers.code_intel import cmd_refactor

        await cmd_refactor(self, args)

    async def _cmd_typify(self, args: str) -> None:
        from velune.cli.handlers.code_intel import cmd_typify

        await cmd_typify(self, args)

    async def _cmd_providers(self, args: str) -> None:
        from velune.cli.handlers.providers import cmd_providers

        await cmd_providers(self, args)

    # Backwards-compat aliases used by the old private API in tests/tools
    async def _restore_active_model(self) -> None:
        from velune.cli.handlers.model import restore_active_model

        restore_active_model(self)

    def _apply_role_overrides_to_orchestrator(self) -> None:
        from velune.cli.handlers.councilmodel import apply_role_overrides_to_orchestrator

        apply_role_overrides_to_orchestrator(self)

    def _poll_and_render_alerts(self) -> None:
        from velune.cli.handlers.council import poll_and_render_alerts

        poll_and_render_alerts(self)

    def _register_plugin_commands(self, plugins) -> None:
        from velune.cli.handlers.plugins import register_plugin_commands

        register_plugin_commands(self, plugins)


async def run_repl(runtime: RuntimeContext) -> None:
    """Coroutine entry point for the REPL session.

    Callers should use ``velune.kernel.entrypoint.launch()`` to drive this from
    a synchronous context; do not call ``asyncio.run`` directly.
    """
    repl = VeluneREPL(runtime)
    await repl.run()
