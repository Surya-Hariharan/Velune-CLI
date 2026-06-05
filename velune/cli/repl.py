"""VeluneREPL — interactive slash-command shell with token usage tracking
and clean Ctrl+C / shutdown handling.

Slash commands (e.g. /setup, /usage, /help) are dispatched through
SlashCommandRegistry.  Every inference response is recorded in
SessionUsage so per-query and session-total costs are always visible.

Ctrl+C during generation stops the stream cleanly; Ctrl+C twice within
2 seconds exits the process.  Background tasks are cancelled on /exit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from rich.console import Console

from velune.execution.cancellation import InferenceGuard
from velune.telemetry.token_tracker import SessionUsage, TokenUsage


@dataclass
class SlashCommand:
    name: str
    description: str
    usage: str
    handler: Callable[[str], Awaitable[None]]
    aliases: list[str] = field(default_factory=list)


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command
        for alias in command.aliases:
            self._commands[alias] = command

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name)

    def all_commands(self) -> list[SlashCommand]:
        seen: set[str] = set()
        result: list[SlashCommand] = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append(cmd)
        return result


class VeluneREPL:
    """Interactive REPL session with slash command dispatch, token tracking,
    and clean cancellation on Ctrl+C."""

    def __init__(self, console: Console | None = None, container=None) -> None:
        self.console = console or Console()
        self.container = container
        self.registry = SlashCommandRegistry()
        self._session_usage = SessionUsage()
        self._guard = InferenceGuard(self.console)
        self._last_interrupt: float = 0.0
        # Populated when the REPL is wired to an active council session
        self._conversation: list | None = None
        self.active_model = None
        self._register_builtin_commands()

    # ------------------------------------------------------------------
    # Command registration
    # ------------------------------------------------------------------

    def _register_builtin_commands(self) -> None:
        self.registry.register(SlashCommand(
            name="setup",
            aliases=["keys"],
            description="Add or update AI provider API keys",
            usage="/setup",
            handler=self._cmd_setup,
        ))
        self.registry.register(SlashCommand(
            name="usage",
            aliases=["cost"],
            description="Show token usage and cost for this session",
            usage="/usage",
            handler=self._cmd_usage,
        ))
        self.registry.register(SlashCommand(
            name="help",
            aliases=["?"],
            description="Show available slash commands",
            usage="/help",
            handler=self._cmd_help,
        ))
        self.registry.register(SlashCommand(
            name="exit",
            aliases=["quit", "q"],
            description="Exit the REPL session",
            usage="/exit",
            handler=self._cmd_exit,
        ))

    # ------------------------------------------------------------------
    # Built-in command handlers
    # ------------------------------------------------------------------

    async def _cmd_setup(self, args: str) -> None:
        from velune.cli.commands.setup import run_setup_wizard
        run_setup_wizard()

    async def _cmd_usage(self, args: str) -> None:
        from rich.table import Table
        usage = self._session_usage
        if not usage.usages:
            self.console.print("[dim]No usage recorded yet.[/dim]")
            return

        table = Table(border_style="dim", padding=(0, 1))
        table.add_column("Provider", style="cyan")
        table.add_column("Tokens", style="white", justify="right")
        table.add_column("Share", style="dim", justify="right")

        by_provider = usage.by_provider()
        total = usage.total_tokens or 1
        for provider, tokens in sorted(by_provider.items(), key=lambda x: x[1], reverse=True):
            pct = (tokens / total) * 100
            table.add_row(provider, f"{tokens:,}", f"{pct:.0f}%")

        self.console.print(table)
        cost_str = f"~${usage.total_cost:.4f}" if usage.total_cost > 0 else "free"
        self.console.print(
            f"\n[bold]Session total:[/bold] {usage.total_tokens:,} tokens · "
            f"{usage.completion_tokens:,} output · {cost_str}"
        )

    async def _cmd_help(self, args: str) -> None:
        from rich.table import Table
        table = Table(border_style="dim", padding=(0, 1))
        table.add_column("Command", style="cyan")
        table.add_column("Aliases", style="dim")
        table.add_column("Description")
        for cmd in self.registry.all_commands():
            aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else "—"
            table.add_row(f"/{cmd.name}", aliases, cmd.description)
        self.console.print(table)

    async def _cmd_exit(self, args: str) -> None:
        if self.container:
            registry = self.container.get("runtime.background_tasks")
            if registry:
                await registry.cancel_all()
        self.console.print("[dim]Session closed.[/dim]")
        raise SystemExit(0)

    # ------------------------------------------------------------------
    # Token usage display
    # ------------------------------------------------------------------

    def _display_usage(
        self,
        model_id: str,
        provider_id: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        usage = TokenUsage.from_response(
            provider_id=provider_id,
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        self._session_usage.add(usage)

        cost_str = f"~${usage.cost_usd:.4f}" if usage.cost_usd > 0 else "free"
        self.console.print(
            f"  [dim]{usage.total_tokens:,} tokens · {cost_str} · "
            f"session: {self._session_usage.summary_line()}[/dim]"
        )

    # ------------------------------------------------------------------
    # Prompt handler (wired to council when available)
    # ------------------------------------------------------------------

    async def _handle_prompt(self, text: str) -> None:
        """Process a free-text prompt.  Wired to the council when available."""
        try:
            async with self._guard.guard():
                # When the council is wired up this block will call
                # provider.stream(request) and check token.is_cancelled
                # after each chunk.  For now display a placeholder.
                self.console.print(
                    "[dim](REPL not yet wired to council — use `velune chat` for now)[/dim]"
                )
                if self._guard._current_token and self._guard._current_token.is_cancelled:
                    return

            prompt_tokens = len(text.encode()) // 4
            completion_tokens = 0
            self._display_usage("unknown", "unknown", prompt_tokens, completion_tokens)
        except KeyboardInterrupt:
            self.console.print("\n[dim]↩ Interrupted.[/dim]")

    # ------------------------------------------------------------------
    # Slash dispatch
    # ------------------------------------------------------------------

    async def dispatch_slash(self, line: str) -> bool:
        """Dispatch a /command line.  Returns True if handled."""
        if not line.startswith("/"):
            return False
        parts = line[1:].split(None, 1)
        name = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        cmd = self.registry.get(name)
        if cmd is None:
            self.console.print(
                f"[yellow]Unknown command: /{name}. Type /help for a list.[/yellow]"
            )
            return True
        await cmd.handler(args)
        return True

    # ------------------------------------------------------------------
    # Shutdown hook
    # ------------------------------------------------------------------

    async def _on_shutdown(self) -> None:
        if self._conversation:
            try:
                from velune.cli.session_manager import save_session
                workspace = (
                    str(self.container.get("runtime.workspace"))
                    if self.container else "."
                )
                model_id = self.active_model.model_id if self.active_model else "unknown"
                session_id = save_session(self._conversation, model_id, workspace)
                self.console.print(f"[dim]Session auto-saved: {session_id}[/dim]")
            except ImportError:
                pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main interactive REPL loop."""
        self.console.print(
            "[bold cyan]Velune REPL[/bold cyan]  [dim](type /help for commands)[/dim]"
        )
        try:
            while True:
                try:
                    line = self.console.input("[bold green]>[/bold green] ").strip()
                except KeyboardInterrupt:
                    now = time.monotonic()
                    if now - self._last_interrupt < 2.0:
                        raise SystemExit(0)
                    self._last_interrupt = now
                    if self._guard._current_token:
                        self._guard.abort()
                        self.console.print("\n[dim]↩ Stopping generation...[/dim]")
                    else:
                        self.console.print(
                            "\n[dim]Ctrl+C — press again to exit or type /exit[/dim]"
                        )
                    continue
                except EOFError:
                    self.console.print("\n[dim]Goodbye.[/dim]")
                    break

                if not line:
                    continue
                if await self.dispatch_slash(line):
                    continue
                await self._handle_prompt(line)
        finally:
            await self._on_shutdown()
