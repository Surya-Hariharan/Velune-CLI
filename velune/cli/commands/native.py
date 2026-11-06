"""``velune native`` — run one task on the native orchestration engine (explicit opt-in).

Off by default. Enable it for the current shell, or in ``velune.toml``::

    PowerShell:  $env:VELUNE_ORCHESTRATION__ENGINE = "on"
    bash/zsh:    export VELUNE_ORCHESTRATION__ENGINE=on
    velune.toml: [orchestration] engine = "on"

then run, for example, ``velune native "Read README.md and summarize it" --provider groq --model
<model-id>``. While it is off this command only says so and exits non-zero: it never falls back
to normal chat, the Council, another provider or another model.

The agent can use one read-only tool (``read_file``). Each call is shown to you and runs only if you
answer an explicit yes; Enter, no, a timeout, end-of-input, an error or a non-interactive terminal all
mean no. The approval covers that one call and is never remembered.

By default one agent does the whole task. ``--plan-review`` selects a longer, opt-in mode: a plan, an
execution step (the only one that may read files, with the same per-call approval) and a review, with
at most one revision. The same provider and model do all three steps, so the review is a second pass
by the same model, not an independent check. Both modes share one time limit (``--timeout``) and one
request budget, and neither falls back to the other, to normal chat, or to another provider or model.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.text import Text

from velune.cli.context import CLIContext

console = Console()
native_cmd = typer.Typer(help="Native orchestration engine (opt-in)")

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130

DISABLED_MESSAGE = (
    "The native orchestration engine is off. Enable it explicitly: set the environment variable "
    'VELUNE_ORCHESTRATION__ENGINE to "on" (PowerShell: $env:VELUNE_ORCHESTRATION__ENGINE = "on"; '
    'bash: export VELUNE_ORCHESTRATION__ENGINE=on) or put [orchestration] engine = "on" in '
    "velune.toml. Nothing else was run."
)


def native_command(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="What you want done (it may read files, with approval)"),
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="Provider id (default: your saved active model's provider)"
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model id (default: your saved active model)"
    ),
    timeout: float = typer.Option(
        120.0, "--timeout", min=1.0, max=3600.0, help="Whole-run time limit in seconds"
    ),
    plan_review: bool = typer.Option(
        False,
        "--plan-review",
        help="Plan, execute and review (same model, at most one revision) instead of one agent",
    ),
) -> None:
    """Run a task on the native engine: read-only, approval-gated, no fallback."""
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    from velune.orchestration.engine.flag import engine_enabled

    if not engine_enabled(cli_context.config):
        _emit_error(cli_context, DISABLED_MESSAGE)
        raise typer.Exit(code=EXIT_FAILED)

    from velune.core.event_loop import submit

    try:
        code = submit(_run(cli_context, task, provider, model, timeout, plan_review))
    except (KeyboardInterrupt, asyncio.CancelledError):
        _emit_error(cli_context, "Cancelled. No further requests were made.")
        raise typer.Exit(code=EXIT_CANCELLED) from None
    except Exception:  # anything unforeseen: a fixed line, never a traceback or exception text
        _emit_error(cli_context, "The native run failed unexpectedly.")
        raise typer.Exit(code=EXIT_FAILED) from None
    if code:
        raise typer.Exit(code=code)


def resolve_choice(provider: str | None, model: str | None) -> tuple[str, str] | str:
    """The explicit provider and model, or a fixed error message. Never a substitute.

    Order: the flags; otherwise the saved active model (the choice made with ``/model use``). A model
    saved for one provider is never paired with a different provider.
    """
    from velune.cli.model_prefs import load_active_model
    from velune.providers.catalog import canonical_provider_id

    saved = load_active_model()
    provider_id = (provider or "").strip() or (saved.provider_id if saved else "")
    model_id = (model or "").strip()
    # Compare canonical ids so a legacy alias of the saved provider still pairs with its own model,
    # while a genuinely different provider never does.
    if (
        not model_id
        and saved
        and provider_id
        and canonical_provider_id(saved.provider_id) == canonical_provider_id(provider_id)
    ):
        model_id = saved.model_id
    if not provider_id or not model_id:
        return (
            "Choose a provider and model: pass --provider and --model, or select one with "
            "/model use. No default was assumed."
        )
    return provider_id, model_id


async def _run(
    cli_context: CLIContext,
    task: str,
    provider: str | None,
    model: str | None,
    timeout: float,
    plan_review: bool = False,
) -> int:
    from velune.orchestration.engine.errors import EngineDisabledError
    from velune.orchestration.engine_adapters.approval_port import confirm_ask
    from velune.orchestration.engine_adapters.native_session import (
        NativeSessionError,
        run_native_plan_review,
        run_native_task,
    )

    choice = resolve_choice(provider, model)
    if isinstance(choice, str):
        _emit_error(cli_context, choice)
        return EXIT_USAGE
    provider_id, model_id = choice

    # Approval needs a person at an interactive terminal. Without one every call is denied. The
    # global --yes flag is deliberately not consulted: it never approves a tool call.
    interactive = not cli_context.json_mode and _is_tty()
    ask = cancel_aware(confirm_ask()) if interactive else None

    try:
        providers = cli_context.container.get("runtime.provider_registry")
        run = run_native_plan_review if plan_review else run_native_task
        result = await run(
            task,
            config=cli_context.config,
            providers=providers,
            provider_id=provider_id,
            model_id=model_id,
            workspace=Path(str(cli_context.workspace)),
            ask=ask,
            wall_clock_s=timeout,
        )
    except EngineDisabledError:
        _emit_error(cli_context, DISABLED_MESSAGE)
        return EXIT_FAILED
    except NativeSessionError as exc:
        _emit_error(cli_context, str(exc))
        return EXIT_FAILED
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception:  # never show raw exception text: it can carry provider or path detail
        _emit_error(cli_context, "The native run failed unexpectedly.")
        return EXIT_FAILED

    if plan_review:
        _render_plan_review(cli_context, result, approvals_possible=ask is not None)
    else:
        _render(cli_context, result, approvals_possible=ask is not None)
    return 0 if result.ok else EXIT_FAILED


def cancel_aware(ask: Any) -> Any:
    """Make Ctrl-C at the approval prompt a real cancellation, not just a "no".

    The prompt widget reports Ctrl-C as a returned ``CANCEL`` value. On its own that would end the
    run as "a tool call was not approved" (exit 1). Raising ``CancelledError`` instead denies the
    call (it is never approved), marks the run cancelled so nothing further starts, and exits 130.
    Esc ("back") and every other non-True answer remain an ordinary denial.
    """
    from velune.cli.interactive.result import CANCEL

    async def wrapped(prompt: Any) -> Any:
        verdict = await ask(prompt)
        if verdict is CANCEL:
            raise asyncio.CancelledError
        return verdict

    return wrapped


def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _render(cli_context: CLIContext, result: Any, *, approvals_possible: bool) -> None:
    if cli_context.json_mode:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "provider": result.provider_id,
                    "model": result.model_id,
                    "turns": result.turns,
                    "tools": [{"tool": t, "status": s} for t, s in result.tools],
                    "text": result.text,
                    "error": result.message or None,
                    "failure_kind": result.failure_kind,
                }
            )
        )
        return
    if result.ok:
        console.print(Text(result.text or ""))
        return
    console.print(Text(f"Native run did not complete: {result.message}", style="bold red"))
    if not approvals_possible and result.failure_kind == "acting_approval_denied":
        console.print(
            Text(
                "No interactive terminal was available, so every tool call was denied.",
                style="dim",
            )
        )


def _render_plan_review(cli_context: CLIContext, result: Any, *, approvals_possible: bool) -> None:
    """Final answer plus a restrained status line. No plan, critique or provider text is shown."""
    if cli_context.json_mode:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "mode": "plan_review",
                    "status": result.status,
                    "provider": result.provider_id,
                    "model": result.model_id,
                    "answer": result.answer,
                    "error": result.message or None,
                    "failure_kind": result.failure_kind,
                    "review_rounds": result.review_rounds,
                    "model_calls": result.model_calls,
                    "provider_attempts": result.provider_attempts,
                    "tool_calls": result.tool_calls,
                    "unsettled_tool_calls": result.unsettled_calls,
                    "same_model_review": result.same_model_review,
                }
            )
        )
        return
    if result.ok:
        console.print(Text(result.answer or ""))
        console.print(
            Text(
                "Plan/review completed ("
                f"{result.review_rounds} draft{'s' if result.review_rounds != 1 else ''}, "
                f"{result.model_calls} model calls). The same model planned, answered and reviewed, "
                "so this is not an independent check.",
                style="dim",
            )
        )
        return
    console.print(Text(f"Plan/review did not complete: {result.message}", style="bold red"))
    if not approvals_possible and result.status == "denied":
        console.print(
            Text(
                "No interactive terminal was available, so every tool call was denied.",
                style="dim",
            )
        )
    if result.unsettled_calls:
        console.print(
            Text(
                "A tool call may still have been running when the run stopped.",
                style="dim",
            )
        )


def _emit_error(cli_context: CLIContext, message: str) -> None:
    if cli_context.json_mode:
        print(json.dumps({"ok": False, "error": message}))
    else:
        console.print(Text(message, style="bold red"))


__all__ = ["cancel_aware", "native_cmd", "native_command", "resolve_choice"]
