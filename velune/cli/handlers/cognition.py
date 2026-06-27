"""Repository cognition slash command handlers: /index (aka /cognition)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.cognition")


def _get_cognition_service(repl: VeluneREPL):
    try:
        return repl.container.get("runtime.repository_cognition")
    except Exception:
        return None


def _cognition_model_ready(repl: VeluneREPL) -> bool:
    if repl.active_model is not None:
        return True
    try:
        from velune.providers.keystore import list_configured_providers

        if list_configured_providers():
            return True
    except Exception:
        pass
    repl.console.print(
        "[yellow]No model configured.[/yellow] "
        "[dim]Use [bold]/model discover[/bold] or [bold]/model connect[/bold].[/dim]"
    )
    return False


async def cmd_cognition(repl: VeluneREPL, args: str) -> None:
    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    cog = _get_cognition_service(repl)
    if cog is None:
        repl.console.print(
            "[red]Repository indexing is unavailable in this session.[/red]  "
            "[dim]→ Run [bold]/doctor[/bold] to diagnose, then restart Velune.[/dim]"
        )
        return
    if sub in ("", "init"):
        await _cognition_run(repl, cog, deep=False, intro=True)
    elif sub == "quick":
        await _cognition_quick(repl, cog)
    elif sub == "standard":
        await _cognition_run(repl, cog, deep=False)
    elif sub in ("deep", "rebuild"):
        await _cognition_run(repl, cog, deep=True)
    elif sub == "status":
        _cognition_status(repl)
    elif sub == "cancel":
        _cognition_cancel(repl)
    else:
        repl.console.print(
            f"[yellow]Unknown /index subcommand: {sub}[/yellow]  "
            "[dim]init | quick | standard | deep | status | cancel | rebuild[/dim]"
        )


async def _cognition_quick(repl: VeluneREPL, cog) -> None:
    if not _cognition_model_ready(repl):
        return
    reason = cog.unsafe_reason()
    if reason:
        repl.console.print(f"[yellow]Cannot analyze — workspace is {reason}.[/yellow]")
        return
    with repl.console.status("[dim]Quick scan (manifests only)...[/dim]"):
        summary = await asyncio.to_thread(cog.quick_summary)
    _render_quick_summary(repl, summary)


def _render_quick_summary(repl: VeluneREPL, summary: dict) -> None:
    from rich.panel import Panel

    from velune.cli import design

    lines = [f"[bold]Workspace[/bold]  {summary.get('root', '?')}"]
    if summary.get("project_type"):
        lines.append(f"[bold]Type[/bold]       {summary['project_type']}")
    tech = summary.get("tech_stack")
    if isinstance(tech, dict):
        for key, val in tech.items():
            if not val:
                continue
            if isinstance(val, (list, tuple)):
                val = ", ".join(map(str, val))
            elif isinstance(val, dict):
                val = ", ".join(f"{k}={v}" for k, v in val.items())
            lines.append(f"[bold]{str(key).capitalize()}[/bold] {val}")
    repl.console.print(Panel("\n".join(lines), title="Quick Cognition", border_style=design.ACCENT))
    repl.console.print(
        "[dim]→ Run [bold]/index standard[/bold] to build a full symbol index.[/dim]"
    )


async def _cognition_run(repl: VeluneREPL, cog, *, deep: bool, intro: bool = False) -> None:
    if intro:
        repl.console.print(
            "[bold]Cognition[/bold] — index this workspace so Velune understands its code."
        )
    if not _cognition_model_ready(repl):
        return
    reason = cog.unsafe_reason()
    if reason:
        repl.console.print(
            f"[yellow]Refusing to index — workspace is {reason}.[/yellow] "
            "[dim]Open a project with [bold]/project open <path>[/bold] first.[/dim]"
        )
        return
    with repl.console.status("[dim]Estimating scope...[/dim]"):
        preview = await cog.preview()
    if preview.get("file_count", 0) == 0:
        repl.console.print("[yellow]No source files found to index.[/yellow]")
        return
    if not _confirm_cognition(repl, preview, deep=deep):
        repl.console.print("[dim]Cancelled.[/dim]")
        return
    await _submit_cognition_job(repl, cog, deep=deep)


def _confirm_cognition(repl: VeluneREPL, preview: dict, *, deep: bool) -> bool:
    from pathlib import Path

    from rich.panel import Panel
    from rich.prompt import Confirm

    from velune.cli import design

    workspace = Path(repl.container.get("runtime.workspace")).name
    files = preview.get("file_count", 0)
    tokens = preview.get("est_tokens", 0)
    secs = files * (0.06 if deep else 0.025) + 1.0
    repl.console.print(
        Panel(
            f"[bold]Workspace[/bold]          {workspace}\n"
            f"[bold]Mode[/bold]               {'deep' if deep else 'standard'}\n"
            f"[bold]Files[/bold]              {files:,}\n"
            f"[bold]Estimated Tokens[/bold]   {_humanize_count(tokens)}\n"
            f"[bold]Estimated Cost[/bold]     Local Processing\n"
            f"[bold]Estimated Duration[/bold] {_format_duration(secs)}",
            title="Cognition Preview",
            border_style=design.ACCENT,
        )
    )
    try:
        if bool(repl.container.get("runtime.auto_accept")):
            return True
    except Exception:
        pass
    try:
        return Confirm.ask("  Proceed?", default=True)
    except Exception:
        return False


def _humanize_count(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _format_duration(seconds) -> str:
    seconds = max(1, round(seconds))
    if seconds < 60:
        return f"~{seconds}s"
    m, s = divmod(seconds, 60)
    return f"~{m}m {s:02d}s"


async def _submit_cognition_job(repl: VeluneREPL, cog, *, deep: bool) -> None:
    from velune.core.task_registry import JobRecord, JobStatus, track

    mode = "deep" if deep else "standard"
    if repl._job_registry is None:
        with repl.console.status(f"[dim]Cognition ({mode})...[/dim]"):
            try:
                if deep:
                    await cog.run_deep()
                else:
                    await cog.run_incremental()
            except Exception as exc:
                repl.console.print(f"[red]Cognition failed:[/red] {exc}")
                return
        repl.console.print(f"[green]Cognition complete ({mode}).[/green]")
        return

    job_id = repl._job_registry.new_id()
    repl._job_registry.register(JobRecord(job_id=job_id, name=f"cognition:{mode}"))
    repl._cognition_job_id = job_id

    def _progress(processed: int, total: int, rel_path: str) -> None:
        repl._job_registry.update(job_id, current_phase=f"{processed}/{total}")

    async def _run_cognition() -> None:
        repl._job_registry.update(job_id, status=JobStatus.RUNNING, current_phase="scanning")
        try:
            if deep:
                snapshot = await cog.run_deep()
                summary = snapshot.summary if snapshot else {}
                preview = (
                    f"{summary.get('total_files', '?')} files, "
                    f"{summary.get('total_symbols', '?')} symbols"
                )
            else:
                delta = await cog.run_incremental(progress_callback=_progress)
                preview = f"{getattr(delta, 'total', 0)} file(s) indexed"
            repl._job_registry.update(
                job_id,
                status=JobStatus.COMPLETED,
                result_preview=preview[:200],
                completed_at=time.monotonic(),
            )
        except asyncio.CancelledError:
            repl._job_registry.update(
                job_id, status=JobStatus.CANCELLED, completed_at=time.monotonic()
            )
            raise
        except Exception as exc:
            repl._job_registry.update(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc)[:200],
                completed_at=time.monotonic(),
            )

    task_obj = asyncio.create_task(_run_cognition(), name=f"cognition-{job_id}")
    repl._job_registry.update(job_id, task=task_obj)
    track(task_obj)
    repl.console.print(
        f"[green]Cognition job submitted:[/green] [cyan]{job_id}[/cyan] [dim]({mode})[/dim]"
    )
    repl.console.print(
        "[dim]Track with [bold]/index status[/bold], [bold]/jobs[/bold], "
        "or [bold]/dashboard[/bold].[/dim]"
    )


def _cognition_status(repl: VeluneREPL) -> None:
    from rich.table import Table

    from velune.cli.constants import JOB_STATUS_STYLES

    if repl._job_registry is None:
        repl.console.print("[dim]No job registry available.[/dim]")
        return
    jobs = [j for j in repl._job_registry.all_jobs() if j.name.startswith("cognition")]
    if not jobs:
        repl.console.print("[dim]No index jobs yet. Run [bold]/index standard[/bold].[/dim]")
        return
    table = Table(border_style="dim", padding=(0, 1))
    table.add_column("Job", style="cyan")
    table.add_column("Mode", style="dim")
    table.add_column("Status")
    table.add_column("Phase", style="dim")
    table.add_column("Result", style="dim")
    for j in jobs:
        mode = j.name.split(":", 1)[-1]
        style = JOB_STATUS_STYLES.get(j.status.value, "")
        status_cell = f"[{style}]{j.status.value}[/{style}]" if style else j.status.value
        table.add_row(
            j.job_id,
            mode,
            status_cell,
            j.current_phase or "—",
            (j.result_preview or j.error or "—")[:48],
        )
    repl.console.print(table)


def _cognition_cancel(repl: VeluneREPL) -> None:
    if repl._job_registry is None:
        repl.console.print("[dim]No job registry available.[/dim]")
        return
    job_id = getattr(repl, "_cognition_job_id", None)
    if job_id and repl._job_registry.cancel(job_id):
        repl.console.print(f"[yellow]Cancelled cognition job {job_id}.[/yellow]")
        return
    for j in repl._job_registry.all_jobs():
        if j.name.startswith("cognition") and j.status.value in ("running", "pending"):
            if repl._job_registry.cancel(j.job_id):
                repl.console.print(f"[yellow]Cancelled cognition job {j.job_id}.[/yellow]")
                return
    repl.console.print("[dim]No running cognition job to cancel.[/dim]")
