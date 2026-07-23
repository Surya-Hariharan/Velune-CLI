"""Council/run/jobs slash command handlers: /run /council /jobs /dashboard + edit pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.council")


async def cmd_run(repl: VeluneREPL, args: str) -> None:
    if "--bg" in args:
        clean = args.replace("--bg", "").strip()
        await _submit_background_job(repl, clean)
        return
    force_tier = None if repl._mode_manager.is_normal() else repl._mode_manager.config.council_tier
    await execute_council_task(repl, args, force_tier=force_tier)


async def cmd_council(repl: VeluneREPL, args: str) -> None:
    await execute_council_task(repl, args, force_tier="full")


_JOB_KINDS = ("task", "cognition", "shell")


async def cmd_jobs(repl: VeluneREPL, args: str) -> None:
    """List background jobs, filter by kind, or cancel one with /jobs cancel <id>.

    ``/jobs`` on its own lists everything; ``/jobs cognition`` (or ``task`` /
    ``shell``) narrows the listing to just that kind — useful once a session
    has a mix of `/run --bg` reasoning jobs, indexing refreshes, and
    background shell commands all in flight at once.
    """
    from rich.table import Table as RichTable

    from velune.cli.constants import JOB_STATUS_STYLES

    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts and parts[0] else ""

    if sub == "cancel":
        job_id = parts[1].strip() if len(parts) > 1 else ""
        if not job_id:
            repl.console.print("[yellow]Usage: /jobs cancel <job-id>[/yellow]")
            return
        if repl._job_registry is None:
            repl.console.print("[red]Job registry unavailable.[/red]")
            return
        if repl._job_registry.cancel(job_id):
            repl.console.print(f"[yellow]Cancelled:[/yellow] {job_id}")
        else:
            repl.console.print(f"[red]Job not found or already finished:[/red] {job_id}")
        return

    kind_filter: str | None = None
    if sub in _JOB_KINDS:
        kind_filter = sub
    elif sub:
        repl.console.print(
            f"[red]Unknown /jobs filter: {sub!r}[/red]  "
            f"[dim]Try: {' | '.join(_JOB_KINDS)} | cancel <id>[/dim]"
        )
        return

    if repl._job_registry is None:
        repl.console.print("[dim]Job registry unavailable.[/dim]")
        return

    jobs = repl._job_registry.all_jobs()
    if kind_filter:
        jobs = [j for j in jobs if getattr(j, "kind", "task") == kind_filter]

    if not jobs:
        if kind_filter:
            repl.console.print(f"[dim]No {kind_filter} jobs.[/dim]")
        else:
            repl.console.print(
                "[dim]No background jobs yet. Use [bold]/run --bg <task>[/bold] to start one.[/dim]"
            )
        return

    table = RichTable(border_style="dim", padding=(0, 1), title=f"Jobs: {kind_filter}" if kind_filter else None)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Kind", style="dim", width=9)
    table.add_column("Task", max_width=42)
    table.add_column("Status", width=11)
    table.add_column("Phase", style="dim", width=16)
    table.add_column("Elapsed", justify="right", width=8)
    table.add_column("Result / Error", style="dim", max_width=32)

    for job in jobs:
        elapsed_s = (job.completed_at or time.monotonic()) - job.submitted_at
        st = job.status.value
        color = JOB_STATUS_STYLES.get(st, "dim")
        preview = job.result_preview or job.error or "—"
        table.add_row(
            job.job_id,
            getattr(job, "kind", "task"),
            job.name,
            f"[{color}]{st}[/{color}]",
            job.current_phase or "—",
            f"{elapsed_s:.0f}s",
            preview[:32],
        )
    repl.console.print(table)


async def cmd_dashboard(repl: VeluneREPL, args: str) -> None:
    """Open the live system dashboard (session, state, jobs, alerts, health)."""
    from pathlib import Path

    from velune._compat import uncancel_task
    from velune.cli.display.dashboard import ProgressDashboard
    from velune.cli.display.system_snapshot import LiveSessionState, build_system_snapshot

    health_monitor = None
    try:
        health_monitor = repl.container.get("runtime.provider_health_monitor")
    except Exception:
        pass

    # Build the static snapshot once — on-disk reads must not run per refresh tick.
    snapshot = None
    try:
        workspace = repl._mcp_registry.workspace or Path.cwd()
        sessions = repl._session_store.list(workspace=str(workspace))
        snapshot = build_system_snapshot(
            Path(workspace),
            plugin_count=repl._plugin_manager.plugin_count,
            mcp_count=len(repl._mcp_registry._entries),
            session_count=len(sessions),
        )
    except Exception:
        snapshot = None

    def _live_state() -> LiveSessionState:
        model = repl.active_model
        return LiveSessionState(
            model_id=model.model_id if model else None,
            provider_id=model.provider_id if model else None,
            mode_label=repl._mode_manager.current.value.upper(),
            context_pct=repl._context_tracker.percentage if model else 0.0,
        )

    dashboard = ProgressDashboard(
        console=repl.console,
        job_registry=repl._job_registry,
        alert_store=repl._alert_store,
        health_monitor=health_monitor,
        snapshot=snapshot,
        live_state=_live_state,
    )
    async with repl._interrupts.foreground():
        try:
            await dashboard.run_until_keypress()
        except asyncio.CancelledError:
            if not repl._interrupts.consume_user_cancelled():
                raise
            task = asyncio.current_task()
            if task is not None:
                uncancel_task(task)


async def _submit_background_job(repl: VeluneREPL, task: str) -> None:
    """Submit *task* as a fire-and-forget background council job."""
    if not task.strip():
        repl.console.print("[yellow]Usage: /run --bg <task>[/yellow]")
        return
    if repl._job_registry is None:
        repl.console.print("[red]Job registry unavailable — cannot run background jobs.[/red]")
        return

    from velune.core.task_registry import JobRecord, JobStatus, track

    job_id = repl._job_registry.new_id()
    job = JobRecord(job_id=job_id, name=task[:60])
    repl._job_registry.register(job)

    async def _run_in_bg() -> None:
        repl._job_registry.update(job_id, status=JobStatus.RUNNING)
        try:
            orchestrator = repl.container.get("runtime.council_orchestrator")
            last_output: str | None = None
            async for milestone in orchestrator.stream(task):
                if hasattr(milestone, "phase") and milestone.phase:
                    repl._job_registry.update(job_id, current_phase=milestone.phase)
                if hasattr(milestone, "message") and milestone.message:
                    last_output = milestone.message

            repl._job_registry.update(
                job_id,
                status=JobStatus.COMPLETED,
                result_preview=(last_output or "")[:200],
                completed_at=time.monotonic(),
            )
            try:
                from velune.events import Event

                bus = repl.container.get("runtime.bus")
                await bus.emit(
                    Event(
                        event_type="job.completed",
                        source="background_runner",
                        data={"job_id": job_id, "name": task[:60]},
                    )
                )
            except Exception:
                pass
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
            try:
                from velune.events import Event

                bus = repl.container.get("runtime.bus")
                await bus.emit(
                    Event(
                        event_type="job.failed",
                        source="background_runner",
                        data={"job_id": job_id, "error": str(exc)[:200]},
                    )
                )
            except Exception:
                pass

    task_obj = asyncio.create_task(_run_in_bg(), name=f"bg-job-{job_id}")
    repl._job_registry.update(job_id, task=task_obj)
    track(task_obj)

    repl.console.print(
        f"[green]Job submitted:[/green] [cyan]{job_id}[/cyan]  [dim]{task[:60]}[/dim]"
    )
    repl.console.print(
        "[dim]Use [bold]/jobs[/bold] to track progress, "
        "[bold]/dashboard[/bold] for live view.[/dim]"
    )


async def execute_council_task(repl: VeluneREPL, task: str, force_tier: str | None) -> None:
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel

    from velune._compat import uncancel_task
    from velune.cli.display.pipeline import PipelineTracker

    if not task.strip():
        repl.console.print("[yellow]Usage: /run <task>  or  /council <task>[/yellow]")
        return

    orchestrator = repl.container.get("runtime.council_orchestrator")
    repo_cognition = repl.container.get("runtime.repository_cognition")

    repl.console.print("[dim]Scanning workspace...[/dim]")
    try:
        snapshot = repo_cognition.get_snapshot() or repo_cognition.index(force=False)
        lines = [f"Root: {snapshot.root_path}"]
        for f in snapshot.files[:20]:
            lines.append(f"  {f.path} ({f.language.value})")
    except Exception:
        pass

    repl.console.print()

    tracker = PipelineTracker()

    _phase_colors: dict[str, str] = {
        "planner": "magenta",
        "coder": "green",
        "reviewer": "yellow",
        "challenger": "red",
        "arbitration": "blue",
        "synthesis": "cyan",
        "context reconstruction": "dim",
        "debate": "orange1",
        "council": "dim",
        "model assignment": "cyan",
    }

    last_run_id: str | None = None
    current_phase: str | None = None
    phase_messages: list[str] = []
    active_live: Live | None = None
    _phase_timings: dict[str, float] = {}

    def make_panel(phase_name: str, messages: list[str]) -> Panel:
        color = _phase_colors.get(phase_name.lower(), "dim")
        label = phase_name.capitalize()
        body = "\n".join(f"  [{color}]{msg}[/{color}]" for msg in messages)
        return Panel(
            body,
            title=f"[bold {color}]{label} Phase[/bold {color}]",
            border_style=color,
            padding=(0, 2),
            expand=True,
        )

    def make_view(phase_name: str, messages: list[str]) -> Group:
        return Group(tracker.render(), "", make_panel(phase_name, messages))

    try:
        async with repl._interrupts.foreground():
            async for milestone in orchestrator.stream(task):
                last_run_id = milestone.run_id
                phase = milestone.phase or "council"
                message = milestone.message

                if phase != current_phase:
                    if active_live:
                        active_live.stop()
                        repl.console.print(make_panel(current_phase, phase_messages))

                    if milestone.elapsed_ms is not None and current_phase:
                        _phase_timings[current_phase] = milestone.elapsed_ms

                    current_phase = phase
                    phase_messages = []
                    tracker.advance(phase)

                    active_live = Live(
                        make_view(current_phase, phase_messages),
                        console=repl.console,
                        refresh_per_second=4,
                        transient=True,
                    )
                    active_live.start()

                phase_messages.append(message)
                if active_live:
                    active_live.update(make_view(current_phase, phase_messages))

            if active_live:
                active_live.stop()
                repl.console.print(make_panel(current_phase, phase_messages))
            tracker.complete()
            repl.console.print()
            repl.console.print(tracker.render())
            if len(_phase_timings) > 1:
                from velune.cli.display.council_view import render_phase_timing_footer

                render_phase_timing_footer(repl.console, _phase_timings)

    except asyncio.CancelledError:
        if not repl._interrupts.consume_user_cancelled():
            raise
        task_obj = asyncio.current_task()
        if task_obj is not None:
            uncancel_task(task_obj)
        if active_live:
            active_live.stop()
        repl.console.print("\n[yellow]Council run interrupted.[/yellow]")
        return
    except KeyboardInterrupt:
        if active_live:
            active_live.stop()
        repl.console.print("\n[yellow]Council run interrupted.[/yellow]")
        return
    except Exception as e:
        if active_live:
            active_live.stop()
        tracker.fail(current_phase)
        repl.console.print(tracker.render())
        from velune.cli.rendering.error_panel import render_error, render_unexpected_error
        from velune.core.errors.catalog import VeluneError

        if isinstance(e, VeluneError):
            repl.console.print(render_error(e))
        else:
            repl.console.print(render_unexpected_error(e))
        return

    if last_run_id:
        state = orchestrator.get_state(last_run_id)
        if state and state.output:
            repl.console.print()
            repl.console.print(
                Panel(
                    state.output,
                    title="[bold cyan]Council Result[/bold cyan]",
                    border_style="cyan",
                    padding=(1, 2),
                )
            )
            repl._conversation.append({"role": "user", "content": f"/run {task}"})
            repl._conversation.append({"role": "assistant", "content": state.output})

            if state.coder_proposal:
                await apply_council_edits(repl, state.coder_proposal, task)


async def apply_council_edits(repl: VeluneREPL, coder_proposal: str, task: str) -> None:
    """Parse structured edit blocks from *coder_proposal* and apply them with user review."""
    from pathlib import Path as _Path

    from velune.execution.edit_formats import EditBlockApplier, parse_with_fallback
    from velune.execution.multi_diff import MultiDiffPreview
    from velune.models.family import detect_family

    workspace = _Path(repl.container.get("runtime.workspace")).resolve()

    family = detect_family(repl.active_model.model_id if repl.active_model else "")
    blocks = parse_with_fallback(coder_proposal, family, workspace_path=workspace)

    if not blocks:
        repl.console.print(
            "[dim]No structured edit blocks detected in coder output — "
            "review the Council Result above and apply changes manually.[/dim]"
        )
        return

    applier = EditBlockApplier(workspace)
    resolved = applier.resolve_all(blocks)

    if not resolved:
        repl.console.print("[yellow]All edit blocks failed to apply (SEARCH not matched).[/yellow]")
        return

    repl.console.print(
        f"\n[bold cyan]{len(resolved)} file change(s) proposed by the Council[/bold cyan]"
    )

    preview = MultiDiffPreview(repl.console)
    file_writes = dict(resolved)
    from velune.execution.diff_preview import DiffDecision

    decisions = await preview.preview_batch(file_writes, auto_accept=False)

    accepted_paths: list[_Path] = []
    if repl._hunk_review_mode:
        from velune.execution.hunk_review import HunkReviewer

        hunk_reviewer = HunkReviewer(repl.console)
        for path, decision in decisions.items():
            if decision != DiffDecision.ACCEPT:
                continue
            file_diff = preview.preview.compute_diff(path, file_writes[path])
            hunks = hunk_reviewer.split_into_hunks(file_diff)
            if len(hunks) <= 1 or file_diff.is_new_file or file_diff.is_deletion:
                applier.write(path, file_writes[path])
            else:
                final_content = await hunk_reviewer.review_hunks(file_diff)
                applier.write(path, final_content)
            accepted_paths.append(path)
    else:
        for path, decision in decisions.items():
            if decision == DiffDecision.ACCEPT:
                applier.write(path, file_writes[path])
                accepted_paths.append(path)

    if not accepted_paths:
        repl.console.print("[dim]No changes applied.[/dim]")
        return

    repl.console.print(f"[green]Applied {len(accepted_paths)} file(s).[/green]")

    committed = await _auto_commit_edits(repl, accepted_paths, task, workspace)
    if committed:
        repl.console.print("[dim]Changes committed. Use [bold]/undo[/bold] to revert.[/dim]")
        await _show_edit_summary_panel(repl, accepted_paths, workspace)


async def _auto_commit_edits(repl: VeluneREPL, paths: list, task: str, workspace) -> bool:
    """Stage *paths* and create a Velune-tagged git commit."""
    import subprocess
    from pathlib import Path as _Path

    workspace = _Path(workspace)
    rel_paths = []
    for p in paths:
        try:
            rel_paths.append(str(_Path(p).relative_to(workspace)))
        except ValueError:
            rel_paths.append(str(p))

    stage = await asyncio.to_thread(
        subprocess.run,
        ["git", "add", "--"] + rel_paths,
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if stage.returncode != 0:
        _log.warning("git add failed: %s", stage.stderr)
        return False

    from velune.repository.commit_message import CommitMessageGenerator

    subject = CommitMessageGenerator().generate([_Path(p) for p in paths], task, workspace)
    message = f"{subject}\n\nCo-authored-by: Velune Council <velune@local>"

    commit = await asyncio.to_thread(
        subprocess.run,
        ["git", "commit", "-m", message],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if commit.returncode != 0:
        _log.debug("git commit failed (maybe nothing staged): %s", commit.stderr)
        return False
    return True


async def _show_edit_summary_panel(repl: VeluneREPL, paths: list, workspace) -> None:
    """Render a compact summary panel after an auto-committed edit session."""
    import subprocess
    from pathlib import Path as _Path

    from rich.panel import Panel

    workspace = _Path(workspace)
    numstat = await asyncio.to_thread(
        subprocess.run,
        ["git", "diff", "--numstat", "HEAD~1"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    added = removed = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                added += int(parts[0])
                removed += int(parts[1])
            except ValueError:
                pass

    files_label = ", ".join(
        str(_Path(p).relative_to(workspace)) if _Path(p).is_absolute() else str(p)
        for p in paths[:5]
    )
    if len(paths) > 5:
        files_label += f" (+{len(paths) - 5} more)"

    summary_lines = [
        f"[green]+{added}[/green] / [red]-{removed}[/red] lines in {len(paths)} file(s)",
        f"[dim]{files_label}[/dim]",
    ]
    repl.console.print(
        Panel(
            "\n".join(summary_lines),
            title="[bold]Edit Summary[/bold]",
            border_style="dim",
            padding=(0, 1),
        )
    )


def poll_and_render_alerts(repl: VeluneREPL) -> None:
    """Drain unread proactive alerts and print them above the prompt."""
    if repl._alert_store is None:
        return
    try:
        unread = repl._alert_store.drain_unread()
    except Exception:
        return
    if not unread:
        return
    _render_alert_panel(repl, unread)


def _render_alert_panel(repl: VeluneREPL, alerts: list) -> None:
    from rich.panel import Panel

    _sev_border = {"danger": "red", "warn": "yellow", "info": "dim"}
    for alert in alerts:
        border = _sev_border.get(alert.severity.value, "dim")
        repl.console.print(
            Panel(
                alert.body,
                title=f"[bold {border}]{alert.title}[/bold {border}]",
                border_style=border,
                padding=(0, 1),
            )
        )
