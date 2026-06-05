"""Batch diff preview — shows multiple file changes sequentially and collects
per-file accept/reject decisions before any writes happen."""

from __future__ import annotations

from pathlib import Path

from velune.execution.diff_preview import DiffDecision, DiffPreview


class MultiDiffPreview:
    def __init__(self, console) -> None:
        self.preview = DiffPreview(console)
        self.console = console

    async def preview_batch(
        self,
        file_writes: dict[Path, str],
        auto_accept: bool = False,
    ) -> dict[Path, DiffDecision]:
        """Show diffs for every pending write and return per-path decisions.

        The caller is responsible for performing the writes only for paths
        whose decision is ACCEPT.
        """
        if not file_writes:
            return {}

        self.console.print(
            f"\n[bold cyan]Review {len(file_writes)} file change(s)[/bold cyan]"
        )
        self.console.print(
            "[dim]Each diff requires your approval before writing.[/dim]\n"
        )

        decisions: dict[Path, DiffDecision] = {}
        auto_accept_all = auto_accept

        for i, (path, content) in enumerate(file_writes.items(), 1):
            self.console.print(f"[dim]Change {i} of {len(file_writes)}[/dim]")
            diff = self.preview.compute_diff(path, content)
            self.preview.render_diff(diff)

            if auto_accept_all:
                decisions[path] = DiffDecision.ACCEPT
                self.console.print("[dim]Auto-accepted.[/dim]")
                continue

            from rich.prompt import Prompt
            action = Prompt.ask(
                "  [dim][a]ccept / [r]eject / [A]ccept all remaining[/dim]",
                choices=["a", "r", "A"],
                default="a",
            )
            if action == "A":
                auto_accept_all = True
                decisions[path] = DiffDecision.ACCEPT
            elif action == "a":
                decisions[path] = DiffDecision.ACCEPT
            else:
                decisions[path] = DiffDecision.REJECT
                self.console.print(f"  [yellow]Skipped: {path}[/yellow]")

        accepted = sum(1 for d in decisions.values() if d == DiffDecision.ACCEPT)
        rejected = len(decisions) - accepted
        self.console.print(
            f"\n[dim]Applied: [green]{accepted} accepted[/green]"
            f" · [red]{rejected} rejected[/red][/dim]"
        )
        return decisions
