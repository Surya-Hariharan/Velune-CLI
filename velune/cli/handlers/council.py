"""Council/run/jobs slash command handlers: /run /council /jobs /dashboard + edit pipeline.

``/council`` (and ``/run``) hand the task to the agent swarm (``velune.swarm``):
a skill and a permission profile are chosen, a team is created, and its
stages run with the session's permission gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.council")

_USAGE = "Usage: /council [--skill <name>] [--permission <name>] <task>  ·  /council show [run]"
_OPTIONS = {"--skill": "skill", "--permission": "permission"}


def parse_council_args(args: str) -> tuple[str, dict[str, str], str]:
    """Split ``/council`` arguments into (task, options, error)."""
    words = args.split()
    options: dict[str, str] = {}
    task: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in _OPTIONS:
            if index + 1 >= len(words):
                return "", {}, f"{word} needs a name"
            options[_OPTIONS[word]] = words[index + 1]
            index += 2
            continue
        task.append(word)
        index += 1
    return " ".join(task), options, ""


async def cmd_run(repl: VeluneREPL, args: str) -> None:
    if "--bg" in args.split():
        clean = " ".join(w for w in args.split() if w != "--bg")
        await _submit_background_job(repl, clean)
        return
    await cmd_council(repl, args)


async def cmd_council(repl: VeluneREPL, args: str) -> None:
    words = args.split()
    if words[:1] == ["show"]:
        show_runs(repl, words[1] if len(words) > 1 else "")
        return
    task, options, error = parse_council_args(args)
    if error:
        repl.console.print(f"[yellow]{error}.[/yellow] [dim]{_USAGE}[/dim]")
        return
    await execute_council_task(repl, task, **options)


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

    table = RichTable(
        border_style="dim", padding=(0, 1), title=f"Jobs: {kind_filter}" if kind_filter else None
    )
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
    """Submit *task* as a fire-and-forget background council job.

    A background job runs in PLAN mode: it never writes files or asks for
    approval while the user is typing something else.
    """
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
        from velune.cli.display.swarm_view import JobSwarmUI
        from velune.permissions import ExecutionMode
        from velune.swarm.setup import run_headless

        repl._job_registry.update(job_id, status=JobStatus.RUNNING)
        try:
            outcome = await run_headless(
                repl.container,
                task,
                mode=ExecutionMode.PLAN,
                preferred=repl.active_model,
                ui=JobSwarmUI(lambda **fields: repl._job_registry.update(job_id, **fields)),
            )
            if not outcome.ok:
                raise RuntimeError(outcome.error)
            repl._job_registry.update(
                job_id,
                status=JobStatus.COMPLETED,
                result_preview=outcome.content[:200],
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
        "[dim]Background jobs only read and answer; nothing is changed. Use [bold]/jobs[/bold] "
        "to track progress, [bold]/dashboard[/bold] for live view.[/dim]"
    )


def serialize_asks(gate: Any) -> None:
    """Let only one agent at a time open an approval prompt on *gate*."""
    lock = asyncio.Lock()
    ask, ask_batch = gate.ask, gate.ask_batch

    if ask is not None:

        async def one_ask(tool_name: str, decision: Any) -> Any:
            async with lock:
                return await ask(tool_name, decision)

        gate.ask = one_ask
    if ask_batch is not None:

        async def one_batch(items: list) -> bool:
            async with lock:
                return await ask_batch(items)

        gate.ask_batch = one_batch


async def _repl_env(repl: VeluneREPL, model: Any, provider: Any) -> tuple[Any, Any]:
    """The agents' environment for a REPL run, and the session gate it uses."""
    from velune.cli.handlers.tool_chat import (
        _make_approver,
        _ToolActivityUI,
        session_gate,
        tool_loop_available,
    )
    from velune.swarm.agent import AgentEnv
    from velune.swarm.setup import repository_summary
    from velune.tools.base.tool import ToolCallContext

    workspace = repl.container.get("runtime.workspace")
    activity = _ToolActivityUI(repl)
    gate = session_gate(repl, activity)
    serialize_asks(gate)
    ctx = ToolCallContext(
        run_id=repl._session_id,
        actor="council",
        workspace=Path(workspace) if workspace else None,
        hook_dispatcher=repl._hook_dispatcher,
        session_id=repl._episodic_session_id or repl._session_id,
    )
    ctx.gate = gate
    env = AgentEnv(
        provider=provider,
        model_id=model.model_id,
        registry=repl.container.get("runtime.tool_registry"),
        mcp_registry=repl._mcp_registry,
        ctx=ctx,
        base_approver=_make_approver(repl, activity),
        context=await repository_summary(repl.container),
        context_window=int(getattr(model, "context_length", 0) or 32_768),
        tools=tool_loop_available(repl, model, provider),
    )
    return env, gate


async def execute_council_task(
    repl: VeluneREPL, task: str, *, skill: str | None = None, permission: str | None = None
) -> None:
    from rich.panel import Panel
    from rich.text import Text

    from velune._compat import uncancel_task
    from velune.cli.display.swarm_view import ReplSwarmUI
    from velune.cli.handlers.tool_chat import current_execution_mode
    from velune.cli.rendering import CustomMarkdown
    from velune.swarm.router import RouteError
    from velune.swarm.run import run_swarm
    from velune.swarm.setup import load_library, runs_root, swarm_settings
    from velune.tools.filesystem.mutation import TurnJournal

    if not task.strip():
        repl.console.print(f"[yellow]{_USAGE}[/yellow]")
        return

    # A Council run is the newest turn from the moment it starts: whatever happens, /rewind
    # must undo this run (or nothing), never an older turn.
    repl._last_turn_journal = None
    model, provider = await repl._resolve_active_model_and_provider()
    if model is None or provider is None:
        repl.console.print("[red]No usable model.[/red] [dim]Pick one with /model.[/dim]")
        return

    ui = ReplSwarmUI(repl.console)
    env, gate = await _repl_env(repl, model, provider)
    manager = getattr(repl, "_plugin_manager", None)
    library = load_library(repl.container, manager.all_skills() if manager is not None else [])
    for warning in library.warnings:
        repl.console.print(f"[yellow]Skipped:[/yellow] [dim]{warning}[/dim]")
    workspace = Path(repl.container.get("runtime.workspace") or Path.cwd())
    journal = TurnJournal()
    try:
        async with repl._interrupts.foreground():
            outcome = await run_swarm(
                task,
                library,
                env,
                runs_root=runs_root(workspace),
                settings=swarm_settings(getattr(repl.runtime, "config", None), model),
                ui=ui,
                skill=skill,
                permission=permission,
                mode=current_execution_mode(repl),
                journal=journal,
            )
    except RouteError as exc:
        repl.console.print(f"[yellow]{exc}[/yellow]")
        return
    except asyncio.CancelledError:
        if not repl._interrupts.consume_user_cancelled():
            raise
        task_obj = asyncio.current_task()
        if task_obj is not None:
            uncancel_task(task_obj)
        repl.console.print("\n[yellow]Council run interrupted.[/yellow]")
        if journal:
            repl.console.print(
                "[dim]Files were changed before it stopped; [bold]/rewind[/bold] undoes them.[/dim]"
            )
        return
    except Exception as exc:
        from velune.cli.rendering.error_panel import render_error, render_unexpected_error
        from velune.core.errors.catalog import VeluneError

        repl.console.print(
            render_error(exc) if isinstance(exc, VeluneError) else render_unexpected_error(exc)
        )
        return
    finally:
        gate.boundary.clear_grants()
        repl._last_turn_journal = journal

    if not outcome.ok:
        lines = [outcome.error, ""] + [
            f"{r.agent_id}: {r.status}" + (f" — {r.error}" if r.error else "")
            for r in outcome.results
        ]
        repl.console.print()
        repl.console.print(
            Panel(
                Text("\n".join(lines).strip()),
                title="[bold red]Council Failed[/bold red]"
                if outcome.status == "failed"
                else "[bold yellow]Council Stopped[/bold yellow]",
                border_style="red" if outcome.status == "failed" else "yellow",
                padding=(1, 2),
            )
        )
        _print_left_behind(repl, outcome)
        repl.console.print(f"[dim]Run saved: /council show {outcome.run_id}[/dim]")
        return

    repl.console.print()
    repl.console.print(
        Panel(
            CustomMarkdown(outcome.content),
            title="[bold cyan]Council Result[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    repl._conversation.append({"role": "user", "content": f"/council {task}"})
    repl._conversation.append({"role": "assistant", "content": outcome.content})
    _print_left_behind(repl, outcome)
    if outcome.plan.output == "diff":
        await apply_council_edits(repl, outcome.content, task)


def _print_left_behind(repl: VeluneREPL, outcome: Any) -> None:
    """Changed files, the saved diagram and warnings, then how to undo."""
    from rich.markup import escape

    from velune.cli.display.swarm_view import outcome_lines

    for line in outcome_lines(outcome):
        repl.console.print(escape(line))
    if outcome.journal:
        repl.console.print(
            "[dim][bold]/rewind[/bold] undoes this run's file changes "
            "(commands that ran are not undone).[/dim]"
        )


def show_runs(repl: VeluneREPL, run_id: str) -> None:
    """``/council show``: list recent runs, or one run's plan and each agent's result."""
    from rich.table import Table

    from velune.swarm.setup import runs_root

    root = runs_root(Path(repl.container.get("runtime.workspace") or Path.cwd()))
    folders = (
        sorted((p for p in root.glob("*") if p.is_dir()), reverse=True) if root.exists() else []
    )
    if not run_id:
        if not folders:
            repl.console.print("[dim]No council runs yet.[/dim]")
            return
        table = Table(box=None, header_style="bold", padding=(0, 1))
        for column in ("Run", "Skill", "Status", "Task"):
            table.add_column(column, overflow="fold")
        for folder in folders[:20]:
            plan, events = _read_run(folder)
            done = next((e for e in reversed(events) if e.get("event") == "done"), {})
            table.add_row(
                folder.name,
                str(plan.get("skill", "?")),
                str(done.get("status", "unfinished")),
                str(plan.get("task", ""))[:70],
            )
        repl.console.print(table)
        return
    folder = root / run_id
    if not folder.is_dir() or folder.parent != root:
        repl.console.print(
            f"[yellow]No run named '{run_id}'.[/yellow] [dim]See /council show[/dim]"
        )
        return
    plan, events = _read_run(folder)
    finished = {e.get("agent"): e for e in events if e.get("event") == "finish"}
    repl.console.print(
        f"[bold]{folder.name}[/bold] [dim]{plan.get('skill')} · {plan.get('permission')} · "
        f"{plan.get('output')}[/dim]\n{plan.get('task', '')}"
    )
    for agent in plan.get("agents", []):
        agent_id = agent.get("id", "")
        event = finished.get(agent_id, {})
        status = event.get("status", "did not run")
        repl.console.print(f"\n[bold]{agent_id}[/bold] [dim]({agent.get('role')}) · {status}[/dim]")
        result = folder / f"{agent_id}.md"
        if result.exists():
            repl.console.print(result.read_text(encoding="utf-8", errors="replace")[:4000])


def _read_run(folder: Path) -> tuple[dict, list[dict]]:
    try:
        plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    events: list[dict] = []
    try:
        for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return plan, events


async def apply_council_edits(repl: VeluneREPL, coder_proposal: str, task: str) -> None:
    """Parse structured edit blocks from *coder_proposal* and apply them with user review."""
    from pathlib import Path as _Path

    from velune.execution.edit_formats import EditBlockApplier, parse_with_fallback
    from velune.execution.multi_diff import MultiDiffPreview
    from velune.models.family import detect_family

    workspace = _Path(repl.container.get("runtime.workspace")).resolve()

    # This is now the most recent turn: a proposal that is never applied (no blocks, refused,
    # every change rejected) changed nothing, and /rewind must not reach back to an older turn.
    repl._last_turn_journal = None

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

    # The edit set goes through the same execution-mode policy as tool calls:
    # PLAN refuses it, AUTO applies it, MANUAL uses the batched diff review
    # below as its approval step.
    auto_accept = await _authorize_council_edits(repl, file_writes)
    if auto_accept is None:
        return
    decisions = await preview.preview_batch(file_writes, auto_accept=auto_accept)

    accepted_paths: list[_Path] = []
    # Journal the Council's writes like an agent turn's, so /rewind can undo them.
    from velune.tools.filesystem.mutation import (
        TurnJournal,
        reset_active_journal,
        set_active_journal,
    )

    journal = TurnJournal()
    journal_token = set_active_journal(journal)
    try:
        await _write_accepted(repl, applier, preview, decisions, file_writes, accepted_paths)
    finally:
        reset_active_journal(journal_token)
        # Replace the previous turn's journal even when nothing was written (see run_tool_chat).
        repl._last_turn_journal = journal

    for note in journal.irreversible:
        repl.console.print(
            f"[yellow]Note:[/yellow] {note} — too large to keep a copy, so [bold]/rewind[/bold] "
            "cannot restore it."
        )

    if not accepted_paths:
        repl.console.print("[dim]No changes applied.[/dim]")
        return

    repl.console.print(f"[green]Applied {len(accepted_paths)} file(s).[/green]")

    if not _council_auto_commit_enabled(repl):
        repl.console.print(
            "[dim]Changes left uncommitted. Review them with [bold]/diff[/bold]; set "
            "[bold]execution.council_auto_commit = true[/bold] to have Velune commit them.[/dim]"
        )
        return

    committed = await _auto_commit_edits(repl, accepted_paths, task, workspace)
    if committed:
        repl.console.print("[dim]Changes committed. Use [bold]/undo[/bold] to revert.[/dim]")
        await _show_edit_summary_panel(repl, accepted_paths, workspace)


async def _write_accepted(repl, applier, preview, decisions, file_writes, accepted_paths) -> None:
    """Write the edits the user accepted (hunk by hunk when hunk review is on)."""
    from velune.execution.diff_preview import DiffDecision
    from velune.tools.filesystem.mutation import FileTooLargeError

    if repl._hunk_review_mode:
        from velune.execution.hunk_review import HunkReviewer

        hunk_reviewer = HunkReviewer(repl.console)
        for path, decision in decisions.items():
            if decision != DiffDecision.ACCEPT:
                continue
            try:
                file_diff = preview.preview.compute_diff(path, file_writes[path])
            except FileTooLargeError:
                # Too large to load and split into hunks: the change the user accepted
                # is applied whole, as for a change with a single hunk.
                applier.write(path, file_writes[path])
                accepted_paths.append(path)
                continue
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


async def _authorize_council_edits(repl: VeluneREPL, file_writes: dict) -> bool | None:
    """Policy decision for a council edit set.

    Returns ``True`` to apply without the review prompt (allowed outright),
    ``False`` to apply through the interactive diff review (needs approval),
    or ``None`` when the policy refuses the edits (nothing is written).
    """
    from velune.cli.handlers.tool_chat import _ToolActivityUI, session_gate
    from velune.permissions import Verdict, authorize
    from velune.permissions.actions import ActionType
    from velune.permissions.boundary import path_action

    gate = session_gate(repl, _ToolActivityUI(repl))
    actions = []
    for path, content in file_writes.items():
        if not content:
            kind = ActionType.DELETE_FILE
        elif path.exists():
            kind = ActionType.MODIFY_FILE
        else:
            kind = ActionType.CREATE_FILE
        actions.append(path_action(kind, path, gate.boundary, "council edit"))
    decision = authorize(actions, gate.state)
    gate._record("council_edits", actions, decision, decision.verdict.value)
    if decision.verdict is Verdict.DENY:
        repl.console.print(f"[yellow]Edits not applied:[/yellow] {decision.reason}")
        return None
    return decision.verdict is Verdict.ALLOW


def _council_auto_commit_enabled(repl: VeluneREPL) -> bool:
    config = repl.container.get_optional("runtime.config")
    execution = getattr(config, "execution", None)
    return bool(getattr(execution, "council_auto_commit", False))


async def _auto_commit_edits(repl: VeluneREPL, paths: list, task: str, workspace) -> bool:
    """Stage *paths* and commit exactly those paths with a Velune-tagged message.

    ``--only`` keeps anything else the user already staged out of the commit.
    """
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
        ["git", "commit", "--only", "-m", message, "--"] + rel_paths,
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
