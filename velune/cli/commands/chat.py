"""Interactive conversational chat entry point with low-latency streaming."""

from __future__ import annotations

import typer
from rich.console import Console

from velune.cli.context import CLIContext
from velune.cognition.firewall import CognitiveFirewall
from velune.models.specializations import CouncilRole
from velune.repository.schemas import RepositorySnapshot

console = Console()


def chat_command(
    ctx: typer.Context,
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Resume a previous session by ID (see: velune session list)",
    ),
) -> None:
    """Start an interactive chat session with your codebase."""
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    from velune.core.event_loop import submit

    submit(_chat_command_async(cli_context, session_id))


async def _chat_command_async(cli_context: CLIContext, resume_id: str | None = None) -> None:
    # 1. Access services from DI container
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    model_registry = container.get("runtime.model_registry")
    model_specialization = container.get("runtime.council_orchestrator").mapper
    repo_cognition = container.get("runtime.repository_cognition")

    # 2. Boot subsystems
    console.print("[bold cyan]⠋[/bold cyan] Bootstrapping Cognitive Operating System kernel...")
    await lifecycle.startup()
    await model_registry.refresh()

    # Onboarding preflight check gate
    from velune.cli.commands.preflight import run_preflight_check

    if not await run_preflight_check(container, console):
        await lifecycle.shutdown()
        return

    # 3. Map Coder role to get fast, single-model access
    roles = model_specialization.map_roles()
    coder_model = roles.get(CouncilRole.CODER)
    if not coder_model:
        from velune.cli.rendering.error_panel import render_error
        from velune.core.errors.catalog import NoModelsAvailableError

        console.print(
            render_error(
                NoModelsAvailableError(
                    cause_override="No Coder model is assigned or found in the model catalog."
                )
            )
        )
        await lifecycle.shutdown()
        return

    provider_registry = container.get("runtime.provider_registry")
    provider = provider_registry.get_or_raise(coder_model.provider_id)

    # 4. Ingest and Scan AST Snapshot
    with console.status("[bold magenta]⚡ Scanning codebase AST structure...[/bold magenta]"):
        snapshot = repo_cognition.index()

    firewall = CognitiveFirewall()
    formatted_snap = _format_snapshot_context_safe(snapshot, firewall)

    # 5. Initialize conversation history with codebase context
    from velune.cli.sessions import SessionStore

    store = SessionStore()
    _system_messages = [
        {
            "role": "system",
            "content": (
                "You are the Lead Coder for the Velune Reasoning Council, serving in low-latency conversational mode.\n"
                "Your objective is to answer questions, explain code, and assist with natural language tasks concisely and directly.\n"
                "You have access to the user's workspace context below.\n\n"
                "WORKSPACE FILE ACCESS: The user can inject file content directly into this conversation.\n"
                "When you need to read a file, list a directory, or search for code, ask them to run:\n"
                "  !read <file_path>      — to inject a file's contents\n"
                "  !ls [dir]              — to list a directory\n"
                "  !grep <pattern> [dir]  — to search for a pattern in source files\n"
                "  !tree [depth]          — to see the workspace directory structure\n"
                "Once they run the command, the output will appear in the conversation and you can work with it.\n"
                "Keep responses focused and do not use verbose pleasantries."
            ),
        },
        {"role": "system", "content": f"Workspace file index (paths only — use !read to get content):\n{formatted_snap}"},
    ]

    # Restore previous conversation if resuming a session.
    _active_session_id: str | None = None
    if resume_id:
        loaded = store.load(resume_id)
        if loaded is None:
            console.print(f"[yellow]Session '{resume_id}' not found — starting a new session.[/yellow]")
            messages = list(_system_messages)
        else:
            meta, prior_turns = loaded
            # Only restore non-system turns so the system prompt is always fresh.
            prior_chat = [t for t in prior_turns if t.get("role") != "system"]
            messages = list(_system_messages) + prior_chat
            _active_session_id = resume_id
            console.print(
                f"[bold green]Resumed session[/bold green] [dim]{meta.id}[/dim] "
                f"— [italic]{meta.title}[/italic] ({len(prior_chat)} prior turns)"
            )
    else:
        messages = list(_system_messages)

    console.print()
    console.print(
        "[bold green]Velune Chat Mode[/bold green] "
        "(type [cyan]!help[/cyan] for commands · [cyan]!exit[/cyan] to quit)"
    )
    console.print(
        "--------------------------------------------------------------------------------"
    )

    while True:
        try:
            user_input = console.input("[bold green]You:[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Exiting chat session. Goodbye![/yellow]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("!exit", "!quit"):
            console.print("[yellow]Exiting chat session. Goodbye![/yellow]")
            break

        if user_input.lower() == "!new":
            chat_turns = [m for m in messages if m.get("role") != "system"]
            if chat_turns and _active_session_id is not None:
                store.save(
                    messages,
                    workspace=str(cli_context.workspace.resolve()),
                    model_id=coder_model.model_id,
                    session_id=_active_session_id,
                )
            messages = list(_system_messages)
            _active_session_id = None
            console.print(
                "[bold green]New session started.[/bold green] Previous conversation cleared."
            )
            continue

        if user_input.lower() == "!sessions":
            sessions = store.list(workspace=str(cli_context.workspace.resolve()), limit=10)
            if not sessions:
                console.print("[dim]No saved sessions for this workspace.[/dim]")
            else:
                console.print("[bold]Recent sessions:[/bold]")
                for m in sessions:
                    console.print(
                        f"  [cyan]{m.id}[/cyan]  {m.title[:50]}  [dim]{m.updated_at[:16].replace('T', ' ')} · {m.turn_count} turns[/dim]"
                    )
                console.print("[dim]Resume with:  velune chat --session <id>[/dim]")
            continue

        if user_input.lower() == "!help":
            console.print(
                "\n[bold]Workspace file commands[/bold] (inject content into the conversation)\n"
                "  [cyan]!ls [dir][/cyan]              List directory contents (default: workspace root)\n"
                "  [cyan]!read <file>[/cyan]           Read a file and inject its contents\n"
                "  [cyan]!grep <pattern> [dir][/cyan]  Search for a pattern across source files\n"
                "  [cyan]!tree [depth][/cyan]          Show workspace directory tree (default depth: 3)\n"
                "\n[bold]Session commands[/bold]\n"
                "  [cyan]!new[/cyan]                   Start a fresh conversation (saves current)\n"
                "  [cyan]!sessions[/cyan]              List recent sessions for this workspace\n"
                "\n[bold]Execution commands[/bold]\n"
                "  [cyan]!run <task>[/cyan]            Escalate to the full Reasoning Council\n"
                "  [cyan]!exit[/cyan]                  Exit chat (session is saved automatically)\n"
            )
            continue

        # ── Workspace file-access commands ────────────────────────────────
        if user_input.startswith("!ls"):
            arg = user_input[3:].strip() or "."
            try:
                from velune.execution.path_guard import resolve_in_workspace

                dir_path = resolve_in_workspace(arg, cli_context.workspace)
                if not dir_path.is_dir():
                    console.print(f"[yellow]Not a directory: {arg}[/yellow]")
                    continue
                entries = sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                lines = []
                for e in entries:
                    tag = "/" if e.is_dir() else ""
                    lines.append(f"  {e.name}{tag}")
                listing = "\n".join(lines)
                rel = str(dir_path.relative_to(cli_context.workspace.resolve()))
                console.print(f"[dim]{rel}/[/dim]")
                console.print(listing)
                # Inject into conversation so the LLM can reason about it
                messages.append({
                    "role": "user",
                    "content": f"[Directory listing of `{rel}`]\n{listing}",
                })
                messages.append({
                    "role": "assistant",
                    "content": f"I can see the directory `{rel}` contains {len(entries)} item(s). What would you like to know about them?",
                })
            except Exception as e:
                console.print(f"[yellow]ls failed: {e}[/yellow]")
            continue

        if user_input.startswith("!read "):
            file_arg = user_input[6:].strip()
            if not file_arg:
                console.print("[yellow]Usage: !read <file_path>[/yellow]")
                continue
            try:
                from velune.execution.path_guard import resolve_in_workspace

                file_path = resolve_in_workspace(file_arg, cli_context.workspace)
                if not file_path.is_file():
                    console.print(f"[yellow]File not found: {file_arg}[/yellow]")
                    continue
                content = file_path.read_text(encoding="utf-8", errors="replace")
                size_kb = file_path.stat().st_size / 1024
                rel = str(file_path.relative_to(cli_context.workspace.resolve()))
                # Truncate very large files to avoid flooding context
                MAX_CHARS = 40_000
                truncated = len(content) > MAX_CHARS
                shown = content[:MAX_CHARS] if truncated else content
                console.print(
                    f"[dim]Reading {rel} ({size_kb:.1f} KB"
                    + (" — truncated to 40 KB" if truncated else "")
                    + ")[/dim]"
                )
                messages.append({
                    "role": "user",
                    "content": f"[Contents of `{rel}`]\n```\n{shown}\n```"
                    + ("\n[File truncated — only first 40 KB shown]" if truncated else ""),
                })
                messages.append({
                    "role": "assistant",
                    "content": f"I've read `{rel}` ({size_kb:.1f} KB). What would you like to know about it?",
                })
            except Exception as e:
                console.print(f"[yellow]read failed: {e}[/yellow]")
            continue

        if user_input.startswith("!grep "):
            parts = user_input[6:].strip().split(None, 1)
            pattern = parts[0] if parts else ""
            search_dir = parts[1].strip() if len(parts) > 1 else "."
            if not pattern:
                console.print("[yellow]Usage: !grep <pattern> [dir][/yellow]")
                continue
            try:
                import re as _re

                from velune.execution.path_guard import resolve_in_workspace
                from velune.repository.scanner import FilesystemScanner

                search_root = resolve_in_workspace(search_dir, cli_context.workspace)
                scanner_g = FilesystemScanner(search_root)
                code_files = scanner_g.scan_code_files()
                regex = _re.compile(pattern, _re.IGNORECASE)
                hits: list[str] = []
                for fp in code_files:
                    try:
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                        for m in regex.finditer(text):
                            line_no = text[: m.start()].count("\n") + 1
                            rel_fp = str(fp.relative_to(cli_context.workspace.resolve()))
                            hits.append(f"  {rel_fp}:{line_no}  {m.group()[:120]}")
                            if len(hits) >= 50:
                                break
                    except Exception:
                        pass
                    if len(hits) >= 50:
                        break
                result_text = "\n".join(hits) if hits else "  (no matches)"
                console.print(
                    f"[dim]grep '{pattern}' in {search_dir} — {len(hits)} match(es)[/dim]"
                )
                console.print(result_text[:3000])
                messages.append({
                    "role": "user",
                    "content": f"[grep results for `{pattern}` in `{search_dir}`]\n{result_text}",
                })
                messages.append({
                    "role": "assistant",
                    "content": f"Found {len(hits)} match(es) for `{pattern}`. What would you like me to do with these results?",
                })
            except Exception as e:
                console.print(f"[yellow]grep failed: {e}[/yellow]")
            continue

        if user_input.startswith("!tree"):
            arg = user_input[5:].strip()
            max_d = int(arg) if arg.isdigit() else 3
            try:
                from velune.repository.scanner import FilesystemScanner
                from rich.tree import Tree as RichTree

                ws = cli_context.workspace.resolve()
                sc = FilesystemScanner(ws)

                def _make_tree(branch: object, cur: Path, d: int) -> None:
                    if d > max_d:
                        return
                    try:
                        items = sorted(cur.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                    except PermissionError:
                        return
                    for item in items:
                        if sc.is_ignored(item):
                            continue
                        if item.is_dir():
                            sub = branch.add(f"[bold]{item.name}/[/bold]")  # type: ignore[attr-defined]
                            _make_tree(sub, item, d + 1)
                        else:
                            branch.add(f"[dim]{item.name}[/dim]")  # type: ignore[attr-defined]

                rt = RichTree(f"[bold]{ws.name}/[/bold]")
                _make_tree(rt, ws, 1)
                console.print(rt)
                # Build text version for LLM
                lines_t: list[str] = []

                def _text_tree(cur: Path, prefix: str, d: int) -> None:
                    if d > max_d:
                        return
                    try:
                        items = sorted(cur.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                    except PermissionError:
                        return
                    for item in items:
                        if sc.is_ignored(item):
                            continue
                        lines_t.append(prefix + item.name + ("/" if item.is_dir() else ""))
                        if item.is_dir():
                            _text_tree(item, prefix + "  ", d + 1)

                _text_tree(ws, "  ", 1)
                tree_text = f"{ws.name}/\n" + "\n".join(lines_t)
                messages.append({
                    "role": "user",
                    "content": f"[Workspace tree (depth {max_d})]\n{tree_text}",
                })
                messages.append({
                    "role": "assistant",
                    "content": f"I can see the workspace structure up to depth {max_d}. What would you like to explore?",
                })
            except Exception as e:
                console.print(f"[yellow]tree failed: {e}[/yellow]")
            continue

        # 6. Escalation handler to full council
        if user_input.startswith("!run "):
            task = user_input[5:].strip()
            if not task:
                console.print(
                    "[yellow]Usage: !run <task>  — please specify a task to execute[/yellow]"
                )
                continue
            console.print(
                f"[bold cyan]Escalating task to full Reasoning Council: '{task}'...[/bold cyan]"
            )

            orchestration_engine = container.get("runtime.orchestration_engine")
            try:
                milestones = []
                async for milestone in orchestration_engine.stream(task):
                    console.print(f"  [bold cyan]•[/bold cyan] {milestone}")
                    milestones.append(milestone)

                run_id = None
                for m in milestones:
                    if hasattr(m, "run_id"):
                        run_id = m.run_id
                        break
                    elif isinstance(m, str) and m.startswith("[") and "]" in m:
                        run_id = m.split("]")[0][1:]
                        break
                state = orchestration_engine.get_state(run_id) if run_id else None
                if state:
                    from velune.orchestration.schemas import ExecutionStatus

                    success = state.status == ExecutionStatus.COMPLETED
                    if success:
                        console.print(
                            f"[bold green]✓ Execution completed successfully: {state.output or 'Done'}[/bold green]"
                        )
                    else:
                        from velune.cli.rendering.error_panel import render_unexpected_error

                        console.print(
                            render_unexpected_error(
                                RuntimeError(state.error or "Unknown execution failure")
                            )
                        )
                else:
                    console.print("[dim]Council run completed but returned no state.[/dim]")
            except Exception as e:
                from velune.cli.rendering.error_panel import render_error, render_unexpected_error
                from velune.core.errors.catalog import VeluneError

                if isinstance(e, VeluneError):
                    console.print(render_error(e))
                else:
                    console.print(render_unexpected_error(e))
            continue

        # 7. Low-latency, streaming conversational execution
        messages.append({"role": "user", "content": user_input})
        from velune.core.types.inference import InferenceRequest

        request = InferenceRequest(
            model_id=coder_model.model_id,
            messages=messages,
            temperature=0.3,
        )

        console.print("[bold cyan]Velune:[/bold cyan] ", end="")
        full_response_content = []

        try:
            capabilities = provider.get_capabilities()
            supports_streaming = getattr(capabilities, "supports_streaming", False) and hasattr(
                provider, "stream"
            )

            if supports_streaming:
                try:
                    async for chunk in provider.stream(request):
                        print(chunk.content, end="", flush=True)
                        full_response_content.append(chunk.content)
                    print()
                except KeyboardInterrupt:
                    print()
                    console.print("[yellow]\nGeneration cancelled by user.[/yellow]")
            else:
                response = await provider.infer(request)
                console.print(response.content)
                full_response_content.append(response.content)

            if full_response_content:
                assistant_response = "".join(full_response_content)
                messages.append({"role": "assistant", "content": assistant_response})

        except Exception as e:
            print()
            from velune.cli.rendering.error_panel import render_error, render_unexpected_error
            from velune.core.errors.catalog import VeluneError

            if isinstance(e, VeluneError):
                console.print(render_error(e))
            else:
                console.print(render_unexpected_error(e))

    # 8. Save session (only if at least one user turn was recorded).
    chat_turns = [m for m in messages if m.get("role") != "system"]
    if chat_turns:
        try:
            saved = store.save(
                messages,
                workspace=str(cli_context.workspace.resolve()),
                model_id=coder_model.model_id,
                session_id=_active_session_id,
            )
            console.print(
                f"[dim]Session saved — id:[/dim] [cyan]{saved.id}[/cyan]  "
                f"[dim]Resume with:[/dim] velune chat --session {saved.id}"
            )
        except Exception:
            pass

    await lifecycle.shutdown()


def _format_snapshot_context_safe(snapshot: RepositorySnapshot, firewall: CognitiveFirewall) -> str:
    """Format snapshot metadata context for query prompt securely."""
    lines = [f"Repository Root: {snapshot.root_path}"]
    lines.append("Codebase Files:")
    for f in snapshot.files[:25]:
        risk_marker = " [⚠ injection-risk]" if f.metadata.get("injection_risk") else ""
        lines.append(f"  - {f.path} ({f.language.value}){risk_marker}")
        if f.symbols:
            safe_syms = [s.name for s in f.symbols[:3] if s.name.isidentifier()]
            if safe_syms:
                lines.append(f"    Symbols: {', '.join(safe_syms)}")
    return "\n".join(lines)
