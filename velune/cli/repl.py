"""VeluneREPL — prompt_toolkit-based interactive REPL (lifecycle & routing only).

All ``_cmd_*`` handler implementations have been extracted into focused modules
under ``velune/cli/handlers/``.  This file owns:
  - State initialisation (``__init__``)
  - Fullscreen UI setup (``_build_fullscreen_ui``)
  - Main event loop (``run``)
  - Lifecycle helpers (``_shutdown_repl``, ``_archive_current_session``, …)
  - Slash command dispatch (``_handle_slash_command``)
  - Thin ``_cmd_*`` delegation stubs (2-3 lines each)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

_log = logging.getLogger("velune.cli.repl")

from velune._compat import uncancel_task
from velune.cli.slash_commands import SlashCommandRegistry
from velune.core.runtime import RuntimeContext
from velune.core.task_registry import track
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
        self._model_switcher = None
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
        # Stable id for this REPL's conversation, reused by autosave and the
        # final archive so a recovered crash maps back to the same slot.
        self._session_id = uuid.uuid4().hex[:8]
        self._last_autosave: float = 0.0
        self._workspace_registry = WorkspaceRegistry()
        self._exit_requested = False
        import time as _time

        self._session_start_time: float = _time.monotonic()
        self._tool_call_count: int = 0
        from velune.tools.safety import ApprovalMode

        self._approval_mode: ApprovalMode = ApprovalMode.ASK
        # Native tool-loop session state: models whose provider rejected the
        # tool payload (skip retrying every turn), and tools the user granted
        # "always allow" for this session.
        self._tools_unsupported_models: set[str] = set()
        self._tool_session_grants: set[str] = set()
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
        self._mcp_watch_task: asyncio.Task | None = None
        from velune.plugins.manager import PluginManager

        self._plugin_manager = PluginManager(
            workspace=Path(workspace_path) if workspace_path else None,
        )
        self._resource_manager = self._build_resource_manager(workspace_path)
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
        # Ollama liveness is refreshed off the UI thread by a background task
        # (see _ollama_probe_loop); the render path only ever reads this flag,
        # so a home-screen redraw never blocks on a socket connect.
        self._ollama_live: bool = False
        self._ollama_probe_task: asyncio.Task | None = None
        # TTL-cached configured-provider snapshot for the home surface, so the
        # render callback does no env/disk enumeration per frame.
        self._providers_cache: tuple[list[str], float] | None = None
        # Cheap change signature for the context-token counter, so the status
        # render only re-tokenizes when the conversation actually changed.
        self._ctx_signature: tuple[int, int] | None = None

    # ------------------------------------------------------------------
    # Workspace trust
    # ------------------------------------------------------------------

    def _ensure_workspace_trust(self) -> bool:
        from velune.core import trust

        workspace = self._mcp_registry.workspace
        if trust.is_trusted(workspace):
            return True

        has_project_config = (
            (workspace / ".mcp.json").exists()
            or (workspace / "velune.toml").exists()
            or (workspace / ".velune" / "hooks.json").exists()
        )
        if not has_project_config:
            return False

        from rich.prompt import Confirm

        self.console.print(
            f"[yellow]This workspace ({workspace}) is not trusted.[/yellow]\n"
            "[dim]It contains project-level hook / MCP / provider config that can "
            "run local commands and redirect API traffic.[/dim]"
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
    # Fullscreen UI
    # ------------------------------------------------------------------

    def _build_fullscreen_ui(self):
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        from velune.cli import design
        from velune.cli.autocomplete import CommandEntry, SlashCompleter
        from velune.cli.command_palette import PALETTE_STYLES, CommandPalette, FavoritesStore
        from velune.cli.fullscreen import FullscreenREPLUI
        from velune.cli.inline_flow import InlineFlow
        from velune.cli.model_switcher import MODEL_SWITCHER_STYLES, ModelSwitcher
        from velune.cli.statusbar import STATUS_BAR_STYLES
        from velune.cli.validators import InlineSyntaxValidator

        style_fragments = {
            "prompt.arrow": f"{design.ACCENT_SOFT} bold",
            **STATUS_BAR_STYLES,
            **PALETTE_STYLES,
            **MODEL_SWITCHER_STYLES,
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

        # Multi-step slash-command flows (/connect, /providers) render into the
        # palette's own float and drive the prompt box, instead of each step
        # standing up its own Application below it.
        flow = InlineFlow()
        self._inline_flow = flow

        palette = CommandPalette(
            self._registry.all_unique(),
            recency_source=completer.recent_commands,
            favorites=FavoritesStore(),
            suppressed=flow.is_active,
        )
        self._command_palette = palette

        model_switcher = ModelSwitcher(
            self, suppressed=lambda: flow.is_active() or palette.is_active()
        )
        self._model_switcher = model_switcher

        kb = KeyBindings()
        palette.add_bindings(kb)
        model_switcher.add_bindings(kb)
        flow.add_bindings(kb)

        def _interrupt(event):
            # A generation or tool turn is running: a single Ctrl+C aborts it.
            # In the fullscreen app SIGINT never fires (raw mode), so this key
            # binding is the only path that can cancel foreground work.
            if self._interrupts.has_foreground:
                self._interrupts.cancel_foreground()
                self._interrupts.reset_exit_window()
                event.app.invalidate()
                return False
            # Idle: keep the "press Ctrl+C again to exit" double-press contract.
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
            command_palette=palette,
            model_switcher=model_switcher,
            inline_flow=flow,
            home_provider=self._home_state,
        )

    async def _reverify_stale_keys(self) -> None:
        """Re-check any stored API key that has aged past its TTL.

        Fire-and-forget on REPL entry. It must never block the prompt and never
        print: a key that turns out to be rejected is surfaced in the status bar
        (``invalid_keys``) rather than written into the transcript, since the
        user did not ask for this and may be mid-thought. Any failure here is
        silent by design — an offline user is not told their keys are broken
        (``verifier`` leaves inconclusive verdicts as STALE).
        """
        try:
            from velune.providers.keystore import list_invalid_providers
            from velune.providers.verifier import reverify_stale

            profile = self.container.get_optional("runtime.profile")
            concurrency = profile.background_concurrency if profile else None
            await reverify_stale(max_concurrency=concurrency)
            self._status_state.invalid_keys = tuple(list_invalid_providers())
        except Exception as exc:
            _log.debug("Background key re-verification failed (non-fatal): %s", exc)

    def _refresh_status_state(self) -> None:
        workspace_path = self.container.get("runtime.workspace")
        folder_name = Path(workspace_path).name if workspace_path else "velune"

        if self.active_model:
            self._context_tracker.max_tokens = self.active_model.context_length
            # Re-tokenizing the whole conversation is real work (tiktoken over
            # all message text); this runs on every render, so only redo it when
            # the conversation actually changed. A cheap length signature —
            # message count plus the size of the last message — is enough to
            # catch appends and in-flight streaming growth.
            convo = self._conversation
            last = convo[-1] if convo else None
            last_len = len(last.get("content", "")) if isinstance(last, dict) else 0
            signature = (len(convo), last_len)
            if signature != self._ctx_signature:
                self._ctx_signature = signature
                self._context_tracker.update(convo)

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

        self._status_state.provider_id = (
            self.active_model.provider_id if self.active_model else None
        )
        self._status_state.git_branch = self._active_branch()
        entries = list(self._mcp_registry._entries.values())
        self._status_state.mcp_total = len(entries)
        self._status_state.mcp_connected = sum(1 for e in entries if e.is_connected)

        # Context threshold crossings feed the proactive watcher (was emitted
        # from the old per-prompt token renderer; the status bar refresh is
        # the surviving per-turn hook).
        from velune.cli import design

        pct = self._status_state.context_pct
        prev_pct = self._prev_ctx_pct
        for threshold in (design.CTX_WARN_PCT, design.CTX_DANGER_PCT):
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

    def _active_branch(self) -> str | None:
        """Current git branch, refreshed at most every 5s. None outside a repo."""
        workspace_path = self.container.get("runtime.workspace")
        if not workspace_path:
            return None
        now = time.monotonic()
        if self._cached_branch is None or (now - self._branch_last_checked) > 5.0:
            from velune.repository.tracker import GitTracker

            try:
                self._cached_branch = GitTracker(Path(workspace_path)).get_active_branch()
            except Exception:
                self._cached_branch = "unknown"
            self._branch_last_checked = now
        return self._cached_branch

    def _configured_providers(self) -> list[str]:
        """Provider IDs with a usable key, cached for 10s. Never probes a socket.

        The env/disk enumeration is cheap but still ran on every home-screen
        redraw; more importantly the ``include_ollama=True`` path used to do a
        blocking socket connect to the Ollama port per frame. Here the disk/env
        list is TTL-cached and Ollama is appended from ``self._ollama_live``,
        which a background task keeps fresh off the UI thread.
        """
        now = time.monotonic()
        cached = self._providers_cache
        if cached is not None and (now - cached[1]) < 10.0:
            base = cached[0]
        else:
            from velune.providers.keystore import list_configured_providers

            try:
                base = list_configured_providers(include_ollama=False)
            except Exception:
                base = []
            self._providers_cache = (base, now)
        if self._ollama_live and "ollama" not in base:
            return ["ollama", *base]
        return list(base)

    async def _ollama_probe_loop(self) -> None:
        """Refresh ``self._ollama_live`` off the UI thread, forever.

        The liveness probe is a synchronous socket connect that must never run
        in a render callback (it stalls typing on machines with no Ollama). This
        low-frequency task owns it instead; the render path only reads the flag.
        """
        from velune.providers.keystore import is_ollama_live

        while True:
            try:
                self._ollama_live = await asyncio.to_thread(is_ollama_live, 0.25)
            except Exception:
                self._ollama_live = False
            await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # Home surface (empty-transcript header + runtime summary)
    # ------------------------------------------------------------------

    def _home_state(self):
        """Build the HomeState shown while the transcript is empty.

        Called on every redraw of the empty home screen, so everything here
        must be cheap: live in-memory attributes plus two TTL-cached
        filesystem probes (memory size, index summary).
        """
        from velune import __version__
        from velune.cli.home import HomeState

        workspace = self.container.get("runtime.workspace")
        workspace_path = str(Path(workspace).resolve()) if workspace else ""

        configured = self._configured_providers()
        local = next((p.title() for p in configured if p in ("ollama", "lmstudio")), None)
        cloud = tuple(p for p in configured if p not in ("ollama", "lmstudio"))

        project_type = None
        if self._project_profile:
            if isinstance(self._project_profile, dict):
                project_type = self._project_profile.get("display_name")
            else:
                project_type = getattr(self._project_profile, "display_name", None)

        entries = list(self._mcp_registry._entries.values())

        return HomeState(
            version=__version__,
            model_id=self.active_model.model_id if self.active_model else None,
            provider=self.active_model.provider_id if self.active_model else None,
            workspace_path=workspace_path,
            git_branch=self._active_branch(),
            project_type=project_type,
            indexed_files=self._indexed_file_count(workspace_path),
            memory_label=self._memory_label(workspace_path),
            mcp_connected=sum(1 for e in entries if e.is_connected),
            mcp_total=len(entries),
            providers=cloud,
            local_runtime=local,
        )

    def _indexed_file_count(self, workspace_path: str) -> int | None:
        """Number of files in .velune/index_state.json, cached for 60s."""
        now = time.monotonic()
        cached = getattr(self, "_index_count_cache", None)
        if cached is not None and (now - cached[1]) < 60.0:
            return cached[0]
        count: int | None = None
        try:
            import json

            state_path = Path(workspace_path) / ".velune" / "index_state.json"
            if state_path.exists():
                data = json.loads(state_path.read_text(encoding="utf-8"))
                count = len(data.get("file_index", {}))
        except Exception:
            count = None
        self._index_count_cache = (count, now)
        return count

    def _memory_label(self, workspace_path: str) -> str | None:
        """Cognitive-memory size summary, cached for 60s."""
        now = time.monotonic()
        cached = getattr(self, "_memory_label_cache", None)
        if cached is not None and (now - cached[1]) < 60.0:
            return cached[0]
        label: str | None = None
        try:
            from velune.core.paths import cognitive_db_path

            db_path = cognitive_db_path(Path(workspace_path) if workspace_path else Path.cwd())
            if db_path.exists():
                label = f"cognitive {db_path.stat().st_size / (1024 * 1024):.1f} MB"
        except Exception:
            label = None
        self._memory_label_cache = (label, now)
        return label

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        from velune.cli.handlers.model import restore_active_model

        ui = self._build_fullscreen_ui()
        self._fullscreen_ui = ui
        self.console = ui.console
        self.runtime.console = ui.console
        self.container.register_instance("runtime.console", ui.console)
        self._stream_renderer.attach_fullscreen_ui(ui)
        ui_task = asyncio.create_task(ui.run(), name="velune.fullscreen_ui")
        ui_task.add_done_callback(lambda _task: ui.request_exit())

        # From here until teardown, palette-styled steps run inside this UI
        # rather than in Applications of their own. Uninstalled in the `finally`
        # below so a `velune setup` in the same process still runs standalone.
        from velune.cli.interactive import host as interactive_host

        interactive_host.install(self._inline_flow)

        restore_active_model(self)
        await self._start_episodic_session()

        # Workspace trust must be resolved BEFORE any hook dispatch: project-level
        # hooks execute arbitrary shell commands, so the gate has to close ahead of
        # the first thing that could run them, not after it.
        trusted = self._ensure_workspace_trust()
        self._hook_dispatcher.set_trusted(trusted)

        model_id = self.active_model.model_id if self.active_model else ""
        _hook_start = await self._hook_dispatcher.dispatch_session_start(
            session_id=self._episodic_session_id or self._hook_dispatcher.session_id,
            model_id=model_id,
        )
        if _hook_start.system_message:
            _log.debug("Session start hook message: %s", _hook_start.system_message)

        # Plugins must load BEFORE connect_all: plugin manifests register their own
        # MCP servers into the registry, and the hot-reload watcher computes its
        # "added" delta from config files only — so a plugin server registered after
        # the connect pass would never connect for the life of the session.
        from velune.cli.handlers.plugins import load_and_register_plugins

        await load_and_register_plugins(self)

        try:
            self._mcp_registry.load_config(trusted=trusted)
            self._mcp_registry.load_env()
            if self._mcp_registry._entries:
                server_count = len(self._mcp_registry._entries)
                results = await self._mcp_registry.connect_all()
                ok = sum(1 for v in results.values() if v)
                if ok:
                    _log.debug("MCP: %s/%s server(s) connected", ok, server_count)
            profile = self.container.get_optional("runtime.profile")
            watch_interval = 30.0 * profile.background_poll_scale if profile else 30.0
            self._mcp_watch_task = asyncio.create_task(
                self._mcp_registry.watch(interval_secs=watch_interval), name="velune.mcp_watch"
            )
        except Exception as exc:
            _log.debug("MCP auto-connect error (non-fatal): %s", exc)

        from velune.cli.handlers.cognition import auto_detect_on_entry

        track(asyncio.create_task(auto_detect_on_entry(self), name="velune.auto_index"))

        track(asyncio.create_task(self._reverify_stale_keys(), name="velune.reverify_keys"))

        self._ollama_probe_task = track(
            asyncio.create_task(self._ollama_probe_loop(), name="velune.ollama_probe")
        )

        self._interrupts.install()
        try:
            self._workspace_registry.touch(Path(self.container.get("runtime.workspace")))
        except Exception:
            pass

        self._notify_orphaned_autosaves()

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
            interactive_host.uninstall(self._inline_flow)
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

        if self._mcp_watch_task is not None and not self._mcp_watch_task.done():
            self._mcp_watch_task.cancel()
            try:
                await self._mcp_watch_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await self._mcp_registry.disconnect_all()
        except Exception as exc:
            _log.debug("MCP disconnect error (non-fatal): %s", exc)

        try:
            await self._resource_manager.disconnect_all()
        except Exception as exc:
            _log.debug("Resource disconnect error (non-fatal): %s", exc)

        self.console.print("[dim]Stopping background tasks...[/dim]")

        # Two independent task universes exist: BackgroundTaskRegistry.submit()
        # and the module-level track(). Only the former was being cancelled here,
        # so tracked tasks — record_turn writes, the Ollama probe loop, turn event
        # emission — kept running into lifecycle shutdown and wrote to a SQLite
        # pool that had already been closed. Drain both.
        from velune.core.task_registry import cancel_tracked

        try:
            await cancel_tracked(timeout=5.0)
        except Exception as exc:
            _log.warning("Tracked task cancellation failed: %s", exc)

        try:
            task_registry = self.container.get("runtime.task_registry")
            await task_registry.cancel_all(timeout=5.0)
        except Exception as exc:
            _log.warning("Background task cancellation failed: %s", exc)
        self.console.print("[dim]Goodbye.[/dim]")

    def _autosave(self, *, force: bool = False) -> None:
        """Crash-guard the live conversation (throttled to ~5s between writes)."""
        if not self._conversation:
            return
        now = time.monotonic()
        if not force and (now - self._last_autosave) < 5.0:
            return
        self._last_autosave = now
        try:
            workspace = str(self.container.get("runtime.workspace") or "")
            self._session_store.autosave(
                self._conversation,
                session_id=self._session_id,
                workspace=workspace,
                model_id=self.active_model.model_id if self.active_model else "unknown",
                mode=self._mode_manager.current.value,
                total_tokens=self.session_tokens,
            )
        except Exception as exc:  # pragma: no cover - best effort
            _log.debug("Autosave failed (non-fatal): %s", exc)

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
            session_id=self._session_id,
        )
        # Clean exit — drop the crash-guard sidecar so it isn't flagged as orphaned.
        self._session_store.clear_autosave(self._session_id)

    # ------------------------------------------------------------------
    # Slash command dispatch
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, text: str) -> None:
        # Imported here, not at module scope: inline_flow pulls in
        # prompt_toolkit, and this module is kept import-light so `velune
        # --version` and friends stay fast.
        from velune.cli import design
        from velune.cli.inline_flow import FlowCancelled

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

        if "confirm" in cmd.permissions:
            from velune.cli.handlers.confirm import confirm_destructive

            if not await confirm_destructive(self, f"Run /{cmd.name}?", default=False):
                self.console.print("[dim]Cancelled.[/dim]")
                return

        try:
            await cmd.handler(args)
        except SystemExit:
            raise
        except FlowCancelled:
            # Ctrl+C inside an interactive flow (/connect's provider picker or
            # key field). The command is abandoned wherever it had got to and
            # the prompt comes straight back — this is explicitly not an exit,
            # and not an error worth a red panel.
            self.console.print(f"[{design.MUTED}]Cancelled.[/{design.MUTED}]")
            # The press was consumed by the flow, so it never reached the
            # "press Ctrl+C again to exit" window. Clear that window anyway:
            # an earlier stray press must not combine with this one to exit the
            # REPL, when all the user asked for was to back out of /connect.
            self._interrupts.reset_exit_window()
        except Exception as e:
            from velune.cli.rendering.error_panel import render_error, render_unexpected_error
            from velune.core.errors.catalog import VeluneError

            if isinstance(e, VeluneError):
                self.console.print(render_error(e))
            else:
                self.console.print(render_unexpected_error(e))
        finally:
            self._refresh_invalid_keys()

    def _refresh_invalid_keys(self) -> None:
        """Re-read which stored keys the provider has rejected.

        The status bar's "<provider> key invalid — /connect" banner was
        populated once, by the background sweep on REPL entry, and never again
        — so connecting that very provider left the banner up, telling the user
        to go and connect something they had just connected.

        Done here, once per slash command, rather than in `_refresh_status_state`:
        that runs on every render, and this reads the credential store from
        disk. A command is the only thing that can change provider state from
        the foreground, and the background sweep already sets this itself.
        """
        try:
            from velune.providers.keystore import list_invalid_providers

            self._status_state.invalid_keys = tuple(list_invalid_providers())
        except Exception as exc:
            _log.debug("Could not refresh invalid-key status (non-fatal): %s", exc)

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

                return json.loads(profile_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from velune.repository.project_type import ProjectTypeDetector

            return ProjectTypeDetector().detect(Path(workspace))
        except Exception:
            return None

    def _display_usage(
        self,
        model: ModelDescriptor,
        tokens: int,
        *,
        completion_tokens: int | None = None,
    ) -> None:
        self.session_tokens += tokens
        cost_per_token = (model.cost_per_1k_tokens or 0.0) / 1000
        query_cost = tokens * cost_per_token
        self.session_cost += query_cost
        self._record_usage_telemetry(model, tokens, completion_tokens)

        parts = [f"[dim]{tokens:,} tokens"]
        if query_cost > 0:
            parts.append(f"~${query_cost:.4f}")
        parts.append(f"session: {self.session_tokens:,} tokens")
        if self.session_cost > 0:
            parts.append(f"~${self.session_cost:.4f}[/dim]")
        else:
            parts.append("[/dim]")

        self.console.print(" · ".join(parts))

    def _record_usage_telemetry(
        self,
        model: ModelDescriptor,
        tokens: int,
        completion_tokens: int | None,
    ) -> None:
        """Feed the per-turn token counts into the telemetry stores.

        Both stores existed with working schemas and readers — ``/usage`` and
        ``/doctor`` — but nothing ever called their record functions, so both
        reported 0 tokens and $0.00 for the life of the product.

        Providers report a total rather than a split, so the completion side is
        estimated from the response text and the prompt side is the remainder.
        Best-effort throughout: telemetry must never break a turn.
        """
        if tokens <= 0:
            return

        completion = max(0, min(completion_tokens if completion_tokens is not None else 0, tokens))
        prompt = max(0, tokens - completion)

        try:
            from velune.telemetry.token_tracker import TokenUsage, current_session

            current_session.add(
                TokenUsage.from_response(
                    provider_id=model.provider_id,
                    model_id=model.model_id,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )
        except Exception as exc:
            _log.debug("Token tracker update failed (non-fatal): %s", exc)

        try:
            from velune.telemetry.usage_tracker import record_usage

            record_usage(
                session_id=self._episodic_session_id or self._session_id,
                provider_id=model.provider_id,
                model_id=model.model_id,
                input_tokens=prompt,
                output_tokens=completion,
            )
        except Exception as exc:
            _log.debug("Usage tracker update failed (non-fatal): %s", exc)

    async def _emit_turn_events(
        self,
        user_text: str,
        response_text: str,
        model_id: str,
        tokens: int,
        intent: Any | None = None,
        intent_confidence: float | None = None,
        context_report: Any | None = None,
    ) -> None:
        try:
            from velune.events import Event

            sections_present: list[str] = []
            data: dict[str, Any] = {
                "user": user_text[:200],
                "response": response_text[:200],
                "model_id": model_id,
                "tokens": tokens,
            }
            if intent is not None:
                data["intent"] = str(intent)
                data["intent_confidence"] = intent_confidence
            if context_report is not None:
                report_dict = context_report.to_dict()
                data["context_report"] = report_dict
                sections_present = report_dict.get("sections_present", [])
                data["three_brain"] = {
                    "retrieved_context_present": "RETRIEVED_CONTEXT" in sections_present,
                    "cognitive_continuity_present": "COGNITIVE_CONTINUITY" in sections_present,
                }
                data["repository_brain"] = {
                    "snapshot_present": "REPOSITORY_SNAPSHOT" in sections_present,
                    "drift_present": "ARCHITECTURAL_DRIFT" in sections_present,
                }

            bus = self.container.get("runtime.bus")
            await bus.emit(
                Event(
                    event_type="turn.completed",
                    source="repl",
                    data=data,
                )
            )
        except Exception:
            pass

    async def _start_episodic_session(self) -> None:
        try:
            episodic = self.container.get("runtime.episodic_session_memory")
            workspace = str(self.container.get("runtime.workspace") or "")
            model_id = self.active_model.model_id if self.active_model else "unknown"
            self._episodic_session_id = await episodic.start_session(
                workspace_root=workspace,
                model=model_id,
                mode=self._mode_manager.current.value,
            )
        except Exception as exc:
            _log.debug("Could not start episodic session: %s", exc)

    async def _end_episodic_session(self) -> None:
        if not self._episodic_session_id:
            return
        try:
            episodic = self.container.get("runtime.episodic_session_memory")
            await episodic.end_session(self._episodic_session_id)
        except Exception as exc:
            _log.debug("Could not end episodic session: %s", exc)
        finally:
            self._episodic_session_id = None

    def _record_turn_async(
        self,
        role: str,
        content: str,
        model_id: str,
        workspace_root: str,
        tokens: int | None = None,
    ) -> None:
        """Fire-and-forget ``MemoryLifecycleManager.record_turn()`` for one turn.

        Tracked via the background task registry so a slow SQLite/embedding
        write never blocks the prompt loop; ``record_turn`` already
        try/excepts each tier write internally and triggers compaction.
        """
        try:
            manager = self.container.get("runtime.memory_lifecycle")
        except Exception:
            return
        if not manager:
            return

        track(
            asyncio.create_task(
                manager.record_turn(
                    session_id=self._episodic_session_id or "unknown",
                    role=role,
                    content=content,
                    model=model_id,
                    tokens=tokens,
                    workspace_root=workspace_root,
                ),
                name=f"record_turn_{role}",
            )
        )

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
            from velune.context.mentions import build_mention_context, parse_mentions

            workspace = Path(self.container.get("runtime.workspace") or ".")
            cleaned_text, mentioned_files, _unresolved = parse_mentions(text, workspace)
            if mentioned_files:
                text = cleaned_text
                mention_context = build_mention_context(mentioned_files)
                if mention_context:
                    self._conversation.append({"role": "system", "content": mention_context})

            _hook_pre = await self._hook_dispatcher.dispatch_user_prompt(
                user_prompt=text,
                session_id=self._episodic_session_id or self._hook_dispatcher.session_id,
            )
            if _hook_pre.transformed_prompt:
                text = _hook_pre.transformed_prompt
            if _hook_pre.system_message:
                self._conversation.append({"role": "system", "content": _hook_pre.system_message})
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

        # Canonical context assembly: ContextAssembler (priority ordering,
        # trust-based trimming, budget enforcement) fed by IntentClassifier,
        # ThreeBrainCoordinator, and the cached Repository Brain snapshot —
        # replaces the old ad-hoc compress/retrieve/fit_messages sequence.
        from velune.cli.handlers.prompt_context import build_turn_context
        from velune.context.budget import ContextBudget

        effective_messages, context_report, intent, intent_confidence = await build_turn_context(
            self, text, model
        )

        budget = ContextBudget.for_chat(self._mode_manager.current, model.context_length)

        request = InferenceRequest(
            model_id=model.model_id,
            messages=effective_messages,
            temperature=mode_config.temperature,
            max_tokens=budget.output_reservation,
        )

        turn_workspace = str(self.container.get("runtime.workspace") or "")
        self._record_turn_async(
            role="user", content=text, model_id=model.model_id, workspace_root=turn_workspace
        )

        # Native tool loop first (models that support function calling can act
        # on the workspace); returns None when unsupported/disabled, in which
        # case the legacy streaming path below is used unchanged.
        from velune.cli.handlers.tool_chat import run_tool_chat

        loop_result = await run_tool_chat(self, model, provider, request)
        if loop_result is not None:
            if loop_result.stop_reason == "interrupted":
                self.console.print()
                self._print_interrupted_frame()
                # Tools that already ran changed the workspace. Keep the user
                # turn and the record of those side effects; only a turn that
                # did nothing at all is safe to erase.
                if loop_result.invocations:
                    from velune.orchestration.tool_loop import format_tool_activity

                    activity = format_tool_activity(loop_result.invocations)
                    if activity:
                        self._conversation.append(
                            {
                                "role": "tool",
                                "content": activity + "\n[turn interrupted by user]",
                            }
                        )
                    self._autosave(force=True)
                elif self._conversation and self._conversation[-1].get("role") == "user":
                    self._conversation.pop()
                return
            assistant_text = loop_result.content
            tokens_used = loop_result.tokens_used

            # Persist what the tools actually did. Previously only the final
            # assistant text survived the turn, so on the next prompt the model
            # had no record that it had read a file or run a command — and read
            # the same files again, every turn.
            if loop_result.invocations:
                from velune.orchestration.tool_loop import format_tool_activity

                activity = format_tool_activity(loop_result.invocations)
                if activity:
                    self._conversation.append({"role": "tool", "content": activity})

            if loop_result.stop_reason == "max_turns" and not assistant_text.strip():
                assistant_text = (
                    "[Velune] The tool loop reached its turn limit before the model "
                    "produced a final answer. Partial work may have been applied — "
                    "see the tool activity above."
                )
                self.console.print(f"[yellow]{assistant_text}[/yellow]")
        else:
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
        self._autosave()
        effective_tokens = tokens_used or len(assistant_text) // 4
        self._display_usage(model, effective_tokens, completion_tokens=len(assistant_text) // 4)
        self._record_turn_async(
            role="assistant",
            content=assistant_text,
            model_id=model.model_id,
            workspace_root=turn_workspace,
            tokens=effective_tokens,
        )
        from velune.core.task_registry import track

        track(
            asyncio.create_task(
                self._emit_turn_events(
                    text,
                    assistant_text,
                    model.model_id,
                    effective_tokens,
                    intent=intent,
                    intent_confidence=intent_confidence,
                    context_report=context_report,
                ),
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

    async def _cmd_trace(self, args: str) -> None:
        from velune.cli.handlers.trace import cmd_trace

        await cmd_trace(self, args)

    async def _cmd_backup(self, args: str) -> None:
        from velune.cli.handlers.recovery import cmd_backup

        await cmd_backup(self, args)

    async def _cmd_restore(self, args: str) -> None:
        from velune.cli.handlers.recovery import cmd_restore

        await cmd_restore(self, args)

    async def _cmd_recover(self, args: str) -> None:
        from velune.cli.handlers.recovery import cmd_recover

        await cmd_recover(self, args)

    def _notify_orphaned_autosaves(self) -> None:
        """On startup, hint that an unsaved/crashed session can be recovered."""
        try:
            workspace = self.container.get("runtime.workspace")
            orphans = self._session_store.list_orphaned_autosaves(workspace=workspace)
        except Exception:
            return
        if not orphans:
            return
        from velune.cli import design

        n = len(orphans)
        self.console.print(
            f"[{design.WARN}]Recovered crash guard:[/{design.WARN}] "
            f"{n} unsaved session{'s' if n > 1 else ''} found — "
            f"run [bold]/recover[/bold] to restore."
        )

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

    def _build_resource_manager(self, workspace_path):
        """Construct the ResourceManager with config, workspace, and approver.

        Isolated in a try/except so a resources-config problem can never block
        the REPL from starting — a bare manager (all connectors, fail-closed
        approver) is always a safe fallback.
        """
        from velune.resources.manager import build_default_manager

        resources_cfg: dict = {}
        try:
            config = self.container.get("runtime.config")
            if config is not None and hasattr(config, "resources"):
                resources_cfg = config.resources.model_dump()
        except Exception as exc:
            _log.debug("Could not load resources config (using defaults): %s", exc)

        manager = build_default_manager(
            config=resources_cfg,
            workspace=Path(workspace_path) if workspace_path else None,
        )
        try:
            from velune.cli.handlers.resources import make_resource_approver

            manager.set_approver(make_resource_approver(self))
        except Exception as exc:
            _log.debug("Could not install interactive resource approver: %s", exc)
        return manager

    async def _cmd_resource(self, args: str) -> None:
        from velune.cli.handlers.resources import cmd_resource

        await cmd_resource(self, args)

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

    async def _cmd_login(self, args: str) -> None:
        from velune.cli.handlers.providers import cmd_login

        await cmd_login(self, args)

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
