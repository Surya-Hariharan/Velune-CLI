"""VeluneREPL — prompt_toolkit-based interactive REPL with token tracking."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText

from velune.cli.slash_commands import SlashCommand, SlashCommandRegistry
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
        from velune.cli.modes import ModeManager
        self._mode_manager = ModeManager()
        self.session_tokens: int = 0
        self.session_cost: float = 0.0
        self._history_file = Path.home() / ".velune" / "repl_history"
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        self._conversation: list[dict] = []
        from velune.orchestration.role_assignments import CouncilRoleMap
        self._assignments_path = Path.home() / ".velune" / "council_roles.json"
        self._role_map = CouncilRoleMap.load(self._assignments_path)
        self._project_profile = self._load_project_profile()
        self._registry = self._build_registry()
        self._apply_role_overrides_to_orchestrator()

    # ------------------------------------------------------------------
    # prompt_toolkit session
    # ------------------------------------------------------------------

    def _build_prompt_session(self) -> PromptSession:
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.styles import Style

        from velune.cli.autocomplete import SlashCompleter

        style = Style.from_dict({
            "prompt.prefix": "#00d7ff bold",
            "prompt.model": "#888888",
            "prompt.mode": "#ff8c00 bold",
            "prompt.arrow": "#00d7ff",
            "ctx.ok":     "#444444",
            "ctx.warn":   "#ff8c00",
            "ctx.danger": "#ff3333 bold",
        })

        try:
            models = self.container.get("runtime.model_registry").list_all()
            model_ids = [m.model_id for m in models]
        except Exception:
            model_ids = []

        completer = SlashCompleter(model_ids=model_ids)

        return PromptSession(
            history=FileHistory(str(self._history_file)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=True,
            complete_in_thread=True,
            style=style,
            mouse_support=False,
            wrap_lines=True,
        )

    def _get_prompt_tokens(self) -> FormattedText:

        from velune.context.window import estimate_tokens

        tokens: list[tuple[str, str]] = [("class:prompt.prefix", "velune")]

        if not self._mode_manager.is_normal():
            color = self._mode_manager.config.prompt_color
            label = self._mode_manager.current.value.upper()
            tokens.append((f"bold fg:{color}", f" [{label}]"))

        if self.active_model:
            tokens.append(("class:prompt.model", f" {self.active_model.model_id}"))

            if self._conversation:
                used = estimate_tokens(
                    " ".join(m["content"] for m in self._conversation)
                )
                limit = self.active_model.context_length
                pct = min(used / limit, 1.0) if limit > 0 else 0.0
                filled = int(pct * 8)
                bar = "█" * filled + "░" * (8 - filled)
                pct_int = int(pct * 100)
                if pct < 0.6:
                    bar_style = "class:ctx.ok"
                elif pct < 0.85:
                    bar_style = "class:ctx.warn"
                else:
                    bar_style = "class:ctx.danger"
                tokens.append((bar_style, f" [{bar} {pct_int}%]"))

        tokens.append(("class:prompt.arrow", " › "))
        return FormattedText(tokens)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        session = self._build_prompt_session()
        self._print_startup_banner()

        while True:
            try:
                raw = await session.prompt_async(self._get_prompt_tokens)
                text = raw.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    await self._handle_slash_command(text)
                else:
                    await self._handle_prompt(text)
            except KeyboardInterrupt:
                self.console.print("\n[dim]Interrupted. Type /exit to quit.[/dim]")
                continue
            except EOFError:
                break

    def _print_startup_banner(self) -> None:
        import httpx

        from velune import __version__
        from velune.cli.banner import render_startup_banner
        from velune.providers.keystore import list_configured_providers

        hardware = self.container.get("runtime.hardware")
        configured = list_configured_providers()

        try:
            r = httpx.get("http://localhost:11434/api/tags", timeout=1.5)
            ollama_live = r.status_code == 200
        except Exception:
            ollama_live = False

        workspace = self.container.get("runtime.workspace")
        workspace_name = Path(workspace).name if workspace else "unknown"
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
            workspace_name=workspace_name,
            active_model_id=model_id,
            version=__version__,
            project_type_name=pt_name,
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

        try:
            await cmd.handler(args)
        except SystemExit:
            raise
        except Exception as e:
            self.console.print(f"[red]/{cmd_name} failed: {e}[/red]")

    def _build_registry(self) -> SlashCommandRegistry:
        registry = SlashCommandRegistry()
        registry.register(SlashCommand(
            name="help", aliases=["h", "?"],
            description="Show all available commands",
            usage="/help",
            handler=self._cmd_help,
        ))
        registry.register(SlashCommand(
            name="exit", aliases=["quit", "q"],
            description="Exit the Velune session",
            usage="/exit",
            handler=self._cmd_exit,
        ))
        registry.register(SlashCommand(
            name="clear", aliases=["cls"],
            description="Clear the terminal screen and conversation context",
            usage="/clear",
            handler=self._cmd_clear,
        ))
        registry.register(SlashCommand(
            name="doctor", aliases=["diag"],
            description="Run environment health checks",
            usage="/doctor",
            handler=self._cmd_doctor,
        ))
        registry.register(SlashCommand(
            name="model", aliases=["m"],
            description="Switch the active model interactively",
            usage="/model [model-id]",
            handler=self._cmd_model,
        ))
        registry.register(SlashCommand(
            name="models", aliases=["ls"],
            description="List all available models",
            usage="/models",
            handler=self._cmd_models,
        ))
        registry.register(SlashCommand(
            name="run", aliases=["r"],
            description="Execute a task through the Reasoning Council",
            usage="/run <task description>",
            handler=self._cmd_run,
        ))
        registry.register(SlashCommand(
            name="council", aliases=["c"],
            description="Force full council tier regardless of task complexity",
            usage="/council <task description>",
            handler=self._cmd_council,
        ))
        registry.register(SlashCommand(
            name="diff", aliases=["d"],
            description="Show uncommitted file changes from the last council run",
            usage="/diff",
            handler=self._cmd_diff,
        ))
        registry.register(SlashCommand(
            name="memory", aliases=["mem"],
            description="Inspect memory tiers and stats",
            usage="/memory [clear|stats]",
            handler=self._cmd_memory,
        ))
        registry.register(SlashCommand(
            name="session", aliases=["s"],
            description="Save, list, or resume sessions",
            usage="/session [save|list|resume <id>|export]",
            handler=self._cmd_session,
        ))
        registry.register(SlashCommand(
            name="context", aliases=["ctx"],
            description="Show context window usage for the current conversation",
            usage="/context",
            handler=self._cmd_context,
        ))
        registry.register(SlashCommand(
            name="optimus", aliases=["fast", "opt"],
            description="Speed mode — instant tier, compressed context, smallest model",
            usage="/optimus",
            handler=self._cmd_optimus,
        ))
        registry.register(SlashCommand(
            name="godly", aliases=["full", "god"],
            description="Max power — full council, largest model, full context",
            usage="/godly",
            handler=self._cmd_godly,
        ))
        registry.register(SlashCommand(
            name="normal", aliases=["reset", "n"],
            description="Return to balanced normal mode",
            usage="/normal",
            handler=self._cmd_normal,
        ))
        registry.register(SlashCommand(
            name="mode", aliases=["status"],
            description="Show the current session mode and its settings",
            usage="/mode",
            handler=self._cmd_mode,
        ))
        registry.register(SlashCommand(
            name="councilmodel", aliases=["cm", "roles"],
            description="Assign specific models to council agent roles",
            usage="/councilmodel [show|reset]",
            handler=self._cmd_councilmodel,
        ))
        registry.register(SlashCommand(
            name="pull", aliases=["download", "get"],
            description="Download an Ollama model interactively",
            usage="/pull [model-id]",
            handler=self._cmd_pull,
        ))
        registry.register(SlashCommand(
            name="delete", aliases=["remove", "rm"],
            description="Delete a locally installed Ollama model",
            usage="/delete <model-id>",
            handler=self._cmd_delete,
        ))
        return registry

    # ------------------------------------------------------------------
    # Built-in command handlers
    # ------------------------------------------------------------------

    async def _cmd_help(self, args: str) -> None:
        from rich.table import Table
        table = Table(show_header=True, border_style="dim", padding=(0, 1))
        table.add_column("Command", style="cyan", width=16)
        table.add_column("Aliases", style="dim", width=12)
        table.add_column("Description", style="white")
        for cmd in self._registry.all_unique():
            aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else "—"
            table.add_row(f"/{cmd.name}", aliases, cmd.description)
        self.console.print(table)

    async def _cmd_exit(self, args: str) -> None:
        self.console.print("[dim]Goodbye.[/dim]")
        raise SystemExit(0)

    async def _cmd_clear(self, args: str) -> None:
        import os
        os.system("cls" if os.name == "nt" else "clear")
        self._conversation = []
        self.console.print("[dim]Screen and conversation context cleared.[/dim]")

    async def _cmd_doctor(self, args: str) -> None:
        from velune.cli.commands.doctor import (
            _check_anthropic_api_key,
            _check_config,
            _check_core_dependencies,
            _check_git,
            _check_gpu,
            _check_groq,
            _check_lm_studio,
            _check_model_benchmarks,
            _check_ollama_connectivity,
            _check_ollama_models,
            _check_openai_api_key,
            _check_python_version,
            _check_qdrant,
            _check_sqlite,
            _check_treesitter,
            _check_velune_dir,
            _check_vram,
            _render_results,
        )
        checks = [
            _check_python_version, _check_core_dependencies,
            _check_ollama_connectivity, _check_ollama_models,
            _check_lm_studio, _check_openai_api_key, _check_anthropic_api_key,
            _check_groq, _check_velune_dir, _check_sqlite, _check_qdrant,
            _check_config, _check_treesitter, _check_git,
            _check_gpu, _check_vram, _check_model_benchmarks,
        ]
        results = []
        with self.console.status("[cyan]Running health checks...[/cyan]"):
            for check_fn in checks:
                try:
                    results.append(check_fn())
                except Exception as e:
                    results.append({
                        "name": check_fn.__name__.replace("_check_", "").replace("_", " ").title(),
                        "status": "error",
                        "message": str(e),
                    })
        _render_results(results)
        failures = sum(1 for r in results if r["status"] == "fail")
        if failures:
            self.console.print(
                f"[red]{failures} check(s) failed.[/red]  "
                "[dim]Run [cyan]velune doctor --fix[/cyan] to attempt automatic fixes.[/dim]"
            )
        else:
            self.console.print("[green]All checks passed.[/green]")

    async def _cmd_model(self, args: str) -> None:
        model_registry = self.container.get("runtime.model_registry")
        provider_registry = self.container.get("runtime.provider_registry")

        # Direct switch when model ID supplied as argument
        if args.strip():
            model = model_registry.get(args.strip())
            if model:
                self.active_model = model
                self.console.print(
                    f"[green]Switched to[/green] [cyan]{model.model_id}[/cyan] "
                    f"[dim]({model.provider_id})[/dim]"
                )
            else:
                self.console.print(f"[red]Model '{args.strip()}' not found.[/red]")
            return

        # Interactive picker
        models = model_registry.list_all()
        if not models:
            self.console.print(
                "[yellow]No models found. Run velune workspace init or "
                "check your Ollama/API configuration.[/yellow]"
            )
            return

        available = [
            m for m in models
            if provider_registry.get(m.provider_id) is not None
        ]
        if not available:
            self.console.print("[yellow]No providers are currently reachable.[/yellow]")
            return

        selected = await self._show_model_picker(available)
        if selected:
            self.active_model = selected
            self.console.print(
                f"[green]✓ Active model:[/green] [cyan]{selected.model_id}[/cyan] "
                f"[dim]{selected.provider_id} · "
                f"ctx {selected.context_length:,} · "
                f"{'local' if selected.is_local else 'cloud'}[/dim]"
            )

    async def _show_model_picker(
        self, models: list[ModelDescriptor]
    ) -> ModelDescriptor | None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        local = [m for m in models if m.is_local]
        cloud = [m for m in models if not m.is_local]
        grouped = local + cloud

        # Pre-select the currently active model if it's in the list
        selected_index = [0]
        if self.active_model:
            for i, m in enumerate(grouped):
                if m.model_id == self.active_model.model_id:
                    selected_index[0] = i
                    break

        result: list[ModelDescriptor | None] = [None]

        def _render_list() -> FormattedText:
            lines: list[tuple[str, str]] = []
            lines.append(("bold", "  Select a model  (↑↓ navigate · Enter select · Esc cancel)\n\n"))
            if local:
                lines.append(("fg:ansiyellow", "  — Local Models —\n"))
            for i, m in enumerate(grouped):
                if not m.is_local and i == len(local):
                    lines.append(("fg:ansiyellow", "\n  — Cloud Models —\n"))
                is_sel = i == selected_index[0]
                is_cur = (
                    self.active_model is not None
                    and m.model_id == self.active_model.model_id
                )
                prefix = "❯ " if is_sel else "  "
                row_style = "bold fg:cyan" if is_sel else ""
                ctx = f"{m.context_length // 1000}k"
                local_cloud = "local" if m.is_local else "cloud"
                label = (
                    f"  {prefix}{m.model_id:<40} "
                    f"[{local_cloud:<5} · {m.speed_tier:<6} · ctx {ctx}]"
                )
                lines.append((row_style, label))
                if is_cur:
                    lines.append(("fg:ansigreen", " (active)"))
                lines.append(("", "\n"))
            return FormattedText(lines)

        kb = KeyBindings()

        @kb.add("up")
        def _up(event) -> None:
            selected_index[0] = (selected_index[0] - 1) % len(grouped)

        @kb.add("down")
        def _down(event) -> None:
            selected_index[0] = (selected_index[0] + 1) % len(grouped)

        @kb.add("enter")
        def _enter(event) -> None:
            result[0] = grouped[selected_index[0]]
            event.app.exit()

        @kb.add("escape")
        @kb.add("c-c")
        def _cancel(event) -> None:
            event.app.exit()

        app = Application(
            layout=Layout(Window(
                content=FormattedTextControl(_render_list, focusable=True),
            )),
            key_bindings=kb,
            full_screen=False,
            mouse_support=False,
        )

        await app.run_async()
        return result[0]

    async def _cmd_models(self, args: str) -> None:
        from rich.table import Table

        from velune.core.types.model import CapabilityLevel

        model_registry = self.container.get("runtime.model_registry")
        all_models = model_registry.list_all()

        if not all_models:
            self.console.print("[yellow]No models discovered yet.[/yellow]")
            return

        table = Table(border_style="dim", padding=(0, 1))
        table.add_column("Model", style="cyan")
        table.add_column("Provider", style="dim")
        table.add_column("Type", style="dim")
        table.add_column("Speed", style="dim")
        table.add_column("Context", style="dim", justify="right")
        table.add_column("Top Skill", style="magenta")

        skill_attrs = ["coding", "reasoning", "planning", "summarization"]
        for m in all_models:
            caps = m.capabilities
            top_skill = "general"
            if caps is not None:
                for attr in skill_attrs:
                    level = getattr(caps, attr, CapabilityLevel.NONE)
                    if isinstance(level, int) and level >= CapabilityLevel.ADVANCED:
                        top_skill = attr
                        break
            is_active = (
                self.active_model is not None
                and m.model_id == self.active_model.model_id
            )
            name_col = f"{m.model_id} [green]✓[/green]" if is_active else m.model_id
            table.add_row(
                name_col,
                m.provider_id,
                "local" if m.is_local else "cloud",
                m.speed_tier,
                f"{m.context_length // 1000}k",
                top_skill,
            )
        self.console.print(table)

    async def _cmd_run(self, args: str) -> None:
        force_tier = (
            None if self._mode_manager.is_normal()
            else self._mode_manager.config.council_tier
        )
        await self._execute_council_task(args, force_tier=force_tier)

    async def _cmd_council(self, args: str) -> None:
        await self._execute_council_task(args, force_tier="full")

    async def _execute_council_task(
        self, task: str, force_tier: str | None
    ) -> None:
        if not task.strip():
            self.console.print(
                "[yellow]Usage: /run <task>  or  /council <task>[/yellow]"
            )
            return

        orchestrator = self.container.get("runtime.council_orchestrator")
        repo_cognition = self.container.get("runtime.repository_cognition")

        # Build lightweight workspace summary for context
        self.console.print("[dim]Scanning workspace...[/dim]")
        try:
            snapshot = repo_cognition.get_snapshot() or repo_cognition.index(force=False)
            lines = [f"Root: {snapshot.root_path}"]
            for f in snapshot.files[:20]:
                lines.append(f"  {f.path} ({f.language.value})")
            repo_context = "\n".join(lines)  # noqa: F841 — available for future prompt enrichment
        except Exception:
            pass

        self.console.print()

        from rich.panel import Panel

        _phase_colors: dict[str, str] = {
            "planner": "magenta",
            "coder": "green",
            "reviewer": "yellow",
            "challenger": "red",
            "arbitration": "blue",
            "synthesis": "cyan",
            "context reconstruction": "dim",
            "debate": "orange1",
        }

        last_run_id: str | None = None

        try:
            async for milestone in orchestrator.stream(task):
                last_run_id = milestone.run_id
                phase = milestone.phase
                message = milestone.message
                color = _phase_colors.get(phase.lower(), "dim") if phase else "dim"
                label = phase.capitalize() if phase else "Council"
                self.console.print(
                    f"  [bold {color}]●[/bold {color}] "
                    f"[{color}]{label}[/{color}]"
                    f"  [dim]{message}[/dim]"
                )
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Council run interrupted.[/yellow]")
            return
        except Exception as e:
            self.console.print(f"[red]Council error: {e}[/red]")
            return

        if last_run_id:
            state = orchestrator.get_state(last_run_id)
            if state and state.output:
                self.console.print()
                self.console.print(Panel(
                    state.output,
                    title="[bold cyan]Council Result[/bold cyan]",
                    border_style="cyan",
                    padding=(1, 2),
                ))
                self._conversation.append({"role": "user", "content": f"/run {task}"})
                self._conversation.append({"role": "assistant", "content": state.output})

    async def _cmd_diff(self, args: str) -> None:
        import subprocess

        from rich.syntax import Syntax

        workspace = self.container.get("runtime.workspace")
        stat = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if not stat.stdout.strip():
            self.console.print("[dim]No uncommitted changes.[/dim]")
            return

        self.console.print(stat.stdout)
        full = subprocess.run(
            ["git", "diff"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if full.stdout:
            self.console.print(Syntax(
                full.stdout[:8000],
                "diff",
                theme="monokai",
                line_numbers=False,
            ))

    async def _cmd_memory(self, args: str) -> None:
        from rich.table import Table

        sub = args.strip().lower()
        working = self.container.get("runtime.working_memory")
        episodic = self.container.get("runtime.episodic_memory")

        if sub == "clear":
            working.clear()
            self.console.print("[green]✓ Working memory cleared.[/green]")
            return

        # Default: stats view
        table = Table(title="Memory Tiers", border_style="dim", padding=(0, 1))
        table.add_column("Tier", style="cyan")
        table.add_column("Status", style="dim")
        table.add_column("Records", style="white", justify="right")
        table.add_column("Notes", style="dim")

        working_turns = len(working.get_turns())
        table.add_row(
            "Tier 1 · Working",
            "[green]active[/green]",
            str(working_turns),
            f"session: {working.session_id}",
        )

        episodic_count = 0
        try:
            episodic_count = len(episodic.get_turns("default"))
        except Exception:
            pass
        table.add_row(
            "Tier 2 · Episodic",
            "[green]active[/green]",
            str(episodic_count),
            "SQLite persisted",
        )

        table.add_row("Tier 3 · Semantic", "[green]active[/green]", "—", "Qdrant local")
        table.add_row("Tier 4 · Graph",    "[green]active[/green]", "—", "SQLite graph")
        table.add_row("Tier 5 · Lineage",  "[green]active[/green]", "—", "Decision + FEL store")
        self.console.print(table)

        recent = working.get_recent_turns(3)
        if recent:
            self.console.print("\n[dim]Recent working memory turns:[/dim]")
            for t in recent:
                preview = t.content[:80].replace("\n", " ")
                self.console.print(f"  [dim]{t.role}:[/dim] {preview}…")

    async def _cmd_session(self, args: str) -> None:
        from pathlib import Path as _Path

        from velune.cli.session_manager import (
            export_session_markdown,
            list_sessions,
            load_session,
            save_session,
        )

        workspace = str(self.container.get("runtime.workspace"))
        model_id = self.active_model.model_id if self.active_model else "unknown"
        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else "save"
        sub_args = parts[1] if len(parts) > 1 else ""

        if sub == "save" or not args.strip():
            session_id = save_session(self._conversation, model_id, workspace)
            self.console.print(
                f"[green]✓ Session saved:[/green] [cyan]{session_id}[/cyan]"
            )

        elif sub == "list":
            from rich.table import Table
            sessions = list_sessions()
            if not sessions:
                self.console.print("[dim]No saved sessions.[/dim]")
                return
            table = Table(border_style="dim", padding=(0, 1))
            table.add_column("ID", style="cyan", width=10)
            table.add_column("Saved", style="dim")
            table.add_column("Model", style="dim")
            table.add_column("Turns", style="dim", justify="right")
            for s in sessions:
                ts = s["timestamp"][:16].replace("T", " ")
                table.add_row(s["id"], ts, s["model_id"], str(s["turns"]))
            self.console.print(table)

        elif sub == "resume":
            if not sub_args:
                self.console.print("[yellow]Usage: /session resume <id>[/yellow]")
                return
            sid = sub_args.strip()
            conv = load_session(sid)
            if conv is None:
                self.console.print(f"[red]Session '{sid}' not found.[/red]")
                return
            self._conversation = conv
            self.console.print(
                f"[green]✓ Resumed session {sid} ({len(conv)} turns loaded)[/green]"
            )

        elif sub == "export":
            target = sub_args.strip()
            if not target:
                target = save_session(self._conversation, model_id, workspace)
            md = export_session_markdown(target)
            if md is None:
                self.console.print(f"[red]Session '{target}' not found.[/red]")
                return
            out_path = _Path.cwd() / f"velune-session-{target}.md"
            out_path.write_text(md, encoding="utf-8")
            self.console.print(f"[green]✓ Exported to:[/green] {out_path}")

        else:
            self.console.print(
                f"[red]Unknown subcommand: {sub!r}[/red]  "
                "[dim]Use save | list | resume <id> | export[/dim]"
            )

    async def _cmd_context(self, args: str) -> None:
        from velune.context.window import estimate_tokens

        if not self._conversation:
            self.console.print("[dim]No conversation context yet.[/dim]")
            return

        used = estimate_tokens(" ".join(m["content"] for m in self._conversation))
        limit = self.active_model.context_length if self.active_model else 8192
        pct = (used / limit) * 100 if limit > 0 else 0.0
        turns = len(self._conversation)
        self.console.print(
            f"[cyan]Context:[/cyan] {used:,} / {limit:,} tokens "
            f"[dim]({pct:.1f}% used · {turns} turns)[/dim]"
        )
        if pct > 85:
            self.console.print(
                "[yellow]⚠ Context window nearly full. "
                "Type /clear to reset conversation.[/yellow]"
            )

    # ------------------------------------------------------------------
    # Mode command handlers
    # ------------------------------------------------------------------

    async def _cmd_optimus(self, args: str) -> None:
        from velune.cli.model_selector import ModeAwareModelSelector
        from velune.cli.modes import SessionMode
        config = self._mode_manager.set_mode(SessionMode.OPTIMUS)
        selector = ModeAwareModelSelector(
            self.container.get("runtime.model_registry"),
            self.container.get("runtime.provider_registry"),
        )
        auto_model = selector.select_for_mode(config, self.active_model)
        if auto_model:
            self.active_model = auto_model
        self.console.print(
            f"[yellow]⚡ OPTIMUS MODE[/yellow] — {config.description}\n"
            f"[dim]Model: {self.active_model.model_id if self.active_model else 'none'} · "
            f"Context cap: {config.max_context_tokens:,} tokens · "
            f"Council: {config.council_tier}[/dim]"
        )

    async def _cmd_godly(self, args: str) -> None:
        from velune.cli.model_selector import ModeAwareModelSelector
        from velune.cli.modes import SessionMode
        config = self._mode_manager.set_mode(SessionMode.GODLY)
        selector = ModeAwareModelSelector(
            self.container.get("runtime.model_registry"),
            self.container.get("runtime.provider_registry"),
        )
        auto_model = selector.select_for_mode(config, self.active_model)
        if auto_model:
            self.active_model = auto_model
        self.console.print(
            f"[magenta]🔮 GODLY MODE[/magenta] — {config.description}\n"
            f"[dim]Model: {self.active_model.model_id if self.active_model else 'none'} · "
            f"Context: unlimited · "
            f"Council: {config.council_tier} · "
            f"Retrieval depth: {config.retrieval_depth}[/dim]"
        )

    async def _cmd_normal(self, args: str) -> None:
        from velune.cli.modes import SessionMode
        config = self._mode_manager.set_mode(SessionMode.NORMAL)
        self.console.print(
            f"[cyan]● NORMAL MODE[/cyan] — {config.description}"
        )

    async def _cmd_mode(self, args: str) -> None:
        from rich.table import Table

        config = self._mode_manager.config
        table = Table(border_style="dim", padding=(0, 1), show_header=False)
        table.add_column("Setting", style="dim", width=22)
        table.add_column("Value", style="white")
        table.add_row("Active mode", f"[bold]{config.mode.value.upper()}[/bold]")
        table.add_row("Description", config.description)
        table.add_row("Council tier", config.council_tier)
        table.add_row("Max context", f"{config.max_context_tokens:,} tokens")
        table.add_row("Compression", "on" if config.context_compression else "off")
        table.add_row("Retrieval depth", str(config.retrieval_depth))
        table.add_row("Critics", "disabled" if config.disable_critics else "enabled")
        table.add_row(
            "Current model",
            self.active_model.model_id if self.active_model else "none"
        )
        self.console.print(table)

    # ------------------------------------------------------------------
    # Council model assignment command handlers
    # ------------------------------------------------------------------

    def _apply_role_overrides_to_orchestrator(self) -> None:
        """Push current role assignments into the orchestrator's mapper overrides."""
        try:
            orchestrator = self.container.get("runtime.council_orchestrator")
            if not orchestrator or not hasattr(orchestrator, "mapper"):
                return
            from velune.models.specializations import CouncilRole
            orchestrator.mapper.overrides.clear()
            for role_str, assignment in self._role_map.assignments.items():
                try:
                    orchestrator.mapper.overrides[CouncilRole(role_str)] = assignment.model_id
                except ValueError:
                    pass  # skip roles not in CouncilRole enum (e.g. "embedding")
            if hasattr(orchestrator, "agent_factory"):
                orchestrator.agent_factory.clear_cache()
        except Exception:
            pass

    async def _cmd_councilmodel(self, args: str) -> None:
        sub = args.strip().lower()
        if sub == "show":
            await self._cmd_councilmodel_show()
            return
        if sub == "reset":
            self._role_map.clear_all()
            self._role_map.save(self._assignments_path)
            self._apply_role_overrides_to_orchestrator()
            self.console.print("[yellow]✓ All council role assignments cleared.[/yellow]")
            return

        model_registry = self.container.get("runtime.model_registry")
        provider_registry = self.container.get("runtime.provider_registry")
        available = [
            m for m in model_registry.list_all()
            if provider_registry.get(m.provider_id) is not None
        ]
        if not available:
            self.console.print("[yellow]No models available. Run /doctor to diagnose.[/yellow]")
            return

        from velune.cli.councilmodel_ui import run_councilmodel_ui
        updated = await run_councilmodel_ui(self._role_map, available, self.console)
        if updated is not None:
            self._role_map = updated
            self._role_map.save(self._assignments_path)
            self._apply_role_overrides_to_orchestrator()

    async def _cmd_councilmodel_show(self) -> None:
        from rich.table import Table
        from velune.orchestration.role_assignments import COUNCIL_ROLES, ROLE_DESCRIPTIONS

        table = Table(border_style="dim", padding=(0, 1))
        table.add_column("Role", style="cyan", width=14)
        table.add_column("Assigned Model", style="white")
        table.add_column("Provider", style="dim")
        table.add_column("Description", style="dim")
        for role in COUNCIL_ROLES:
            assignment = self._role_map.get(role)
            model_str = assignment.model_id if assignment else "[dim]auto-routed[/dim]"
            provider_str = assignment.provider_id if assignment else "—"
            table.add_row(
                role, model_str, provider_str,
                ROLE_DESCRIPTIONS.get(role, "")[:45],
            )
        self.console.print(table)
        if not self._role_map.assignments:
            self.console.print(
                "[dim]No custom assignments. Use /councilmodel to assign.[/dim]"
            )

    # ------------------------------------------------------------------
    # Ollama pull / delete command handlers
    # ------------------------------------------------------------------

    async def _cmd_pull(self, args: str) -> None:
        from velune.providers.ollama_manager import OllamaManager
        manager = OllamaManager()

        if not await manager.is_running():
            self.console.print(
                "[red]Ollama is not running.[/red]\n"
                "[dim]Start it with: ollama serve[/dim]"
            )
            return

        if args.strip():
            success = await manager.pull_model(args.strip(), self.console)
            if success:
                await self._refresh_model_registry()
        else:
            from velune.cli.pull_ui import run_pull_ui
            local_models = await manager.list_local_models()
            hardware = self.container.get("runtime.hardware")
            ram_gb = float(hardware.total_ram_gb) if hardware else 16.0
            chosen = await run_pull_ui(local_models, ram_gb, self.console)
            if chosen:
                if chosen in local_models:
                    self.console.print(f"[yellow]{chosen} is already installed.[/yellow]")
                    return
                success = await manager.pull_model(chosen, self.console)
                if success:
                    await self._refresh_model_registry()

    async def _cmd_delete(self, args: str) -> None:
        if not args.strip():
            self.console.print("[yellow]Usage: /delete <model-id>[/yellow]")
            return
        from rich.prompt import Confirm
        from velune.providers.ollama_manager import OllamaManager
        model_id = args.strip()
        confirm = Confirm.ask(
            f"  Delete [cyan]{model_id}[/cyan] from Ollama? This cannot be undone.",
            default=False,
        )
        if not confirm:
            return
        manager = OllamaManager()
        if await manager.delete_model(model_id):
            self.console.print(f"[green]✓ Deleted: {model_id}[/green]")
            await self._refresh_model_registry()
        else:
            self.console.print(f"[red]Failed to delete {model_id}[/red]")

    async def _refresh_model_registry(self) -> None:
        model_registry = self.container.get("runtime.model_registry")
        if not model_registry:
            return
        try:
            await model_registry.refresh()
            count = len(model_registry.list_all())
            self.console.print(
                f"[dim]Model registry refreshed: {count} models available.[/dim]"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Prompt handler
    # ------------------------------------------------------------------

    async def _handle_prompt(self, text: str) -> None:
        from rich.live import Live
        from rich.markdown import Markdown

        from velune.core.types.inference import InferenceRequest

        model, provider = await self._resolve_active_model_and_provider()
        if not model or not provider:
            self.console.print(
                "[red]No model configured. Run /model to select one "
                "or /doctor to diagnose.[/red]"
            )
            return

        # Inject project-aware system prompt on the very first turn
        if not self._conversation and self._project_profile:
            try:
                from velune.repository.project_type import PROJECT_SYSTEM_PROMPTS, ProjectType
                pt_value = (
                    self._project_profile.get("project_type")
                    if isinstance(self._project_profile, dict)
                    else self._project_profile.project_type.value
                )
                addon = PROJECT_SYSTEM_PROMPTS.get(ProjectType(pt_value), "")
                if addon:
                    self._conversation.append({
                        "role": "system",
                        "content": f"You are a coding assistant. {addon}",
                    })
            except Exception:
                pass

        self._conversation.append({"role": "user", "content": text})

        mode_config = self._mode_manager.config

        if mode_config.context_compression and self._conversation:
            from velune.context.extractive import compress_conversation
            self._conversation = compress_conversation(
                self._conversation,
                max_tokens=mode_config.max_context_tokens,
            )

        request = InferenceRequest(
            model_id=model.model_id,
            messages=self._conversation[-50:],   # Hard cap at 50 turns
            temperature=mode_config.temperature,
            max_tokens=4096,
        )

        full_content: list[str] = []
        tokens_used = 0

        try:
            capabilities = provider.get_capabilities()
            supports_stream = getattr(capabilities, "supports_streaming", False)

            if supports_stream:
                buffer = ""
                with Live("", console=self.console, refresh_per_second=12,
                          vertical_overflow="visible") as live:
                    async for chunk in provider.stream(request):
                        if chunk.content:
                            buffer += chunk.content
                            full_content.append(chunk.content)
                            live.update(Markdown(buffer))
            else:
                with self.console.status("[cyan]Thinking...[/cyan]"):
                    response = await provider.infer(request)
                full_content.append(response.content)
                tokens_used = response.tokens_used
                self.console.print(Markdown(response.content))

        except KeyboardInterrupt:
            self.console.print("\n[dim]Generation stopped.[/dim]")
            return

        assistant_text = "".join(full_content)
        self._conversation.append({"role": "assistant", "content": assistant_text})
        self._display_usage(model, tokens_used or len(assistant_text) // 4)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        """Load the cached project profile for the current workspace, or auto-detect."""
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


def run_repl(runtime: RuntimeContext) -> None:
    repl = VeluneREPL(runtime)
    asyncio.run(repl.run())
