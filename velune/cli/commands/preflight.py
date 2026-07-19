"""Preflight check validation for model availability and workspace initialization.

Two shapes of command depend on this gate:

* **Codebase commands** (``chat``, ``run``) reason *about* the current project,
  so they need a real workspace: a git repository plus an ``.velune`` index.
  They call :func:`run_preflight_check` with the default ``require_workspace=True``.
* **One-off commands** (``ask``) answer a single question and must work
  anywhere — including a brand-new user's empty home directory. They pass
  ``require_workspace=False`` so only the "is a model reachable?" check runs.

In every case the *only* hard requirement is a reachable model. When none is
configured we route the user to the right next step: ``velune setup`` for a
fresh install (no providers at all) versus ``velune models scan`` when
providers exist but no models have been discovered yet.
"""

from __future__ import annotations

from pathlib import Path

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from velune.kernel.registry import ServiceContainer


def _no_models_issue() -> str:
    """Return a provider-aware "no models" issue string with the right fix.

    A brand-new user with zero providers is pointed at the guided ``velune
    setup`` wizard; a user who has configured providers but not yet discovered
    models is pointed at ``velune models scan``.
    """
    try:
        from velune.providers.keystore import list_configured_providers

        configured = list_configured_providers()
    except Exception:
        configured = []

    if configured:
        return (
            "Providers are configured but no models have been discovered yet.\n"
            "  [bold white]Fix:[/bold white] Discover models across your providers:\n"
            "       [bold green]velune models scan --probe[/bold green]"
        )
    return (
        "No AI provider is configured yet.\n"
        "  [bold white]Fix:[/bold white] Run the 2-minute guided setup to connect a provider:\n"
        "       [bold green]velune setup[/bold green]   [dim](or [bold]velune onboard[/bold])[/dim]"
    )


async def run_preflight_check(
    container: ServiceContainer,
    console: Console | None = None,
    *,
    require_workspace: bool = True,
) -> bool:
    """Run preflight checks for models and (optionally) workspace state.

    Parameters
    ----------
    container:
        The runtime service container.
    console:
        Console for rendering failure panels. ``None`` suppresses output
        (e.g. JSON mode), in which case only the boolean result matters.
    require_workspace:
        When ``True`` (default) the current directory must be an initialized
        git workspace with an ``.velune`` index — the contract for codebase
        commands. When ``False`` those checks are skipped entirely so a one-off
        question works in any directory; only model availability is enforced.

    Returns
    -------
    bool
        ``True`` if all required checks pass, else ``False`` after rendering a
        panel with copy-pasteable fix commands.
    """
    issues = []

    # 1. Workspace checks — only for codebase commands.
    if require_workspace:
        workspace = container.get("runtime.workspace")
        if not isinstance(workspace, Path):
            workspace = Path(workspace)

        # Guard: workspace must exist and be a git repository. On a brand-new
        # install neither will be true — show a single targeted message and bail
        # early rather than cascading through checks that will all fail.
        if not workspace.exists() or not (workspace / ".git").exists():
            if console:
                console.print()
                console.print(
                    Panel(
                        Text.from_markup(
                            "This doesn't look like a code project yet. Navigate to your project\n"
                            "folder and run [bold green]velune init[/bold green] first.\n\n"
                            '[dim]Just have a question? [bold]velune ask "..."[/bold] works anywhere.[/dim]'
                        ),
                        title="[bold yellow]Not a Project Directory[/bold yellow]",
                        border_style="yellow",
                        box=ROUNDED,
                        padding=(1, 2),
                    )
                )
                console.print()
            return False

        # We check for the presence of the Tree-sitter AST index folder or `.velune` directory structure
        if not (workspace / ".velune" / "index").exists():
            issues.append(
                "Workspace has not been indexed yet.\n"
                "  [bold white]Fix:[/bold white] Run the initialization command to parse the codebase:\n"
                "       [bold green]velune init[/bold green]"
            )

    # 2. Model availability — always required. A model whose provider has
    # actively rejected its key is not reachable: filter it out here and say
    # so, rather than letting the council burn a full retry cycle against
    # guaranteed 401s and returning garbage.
    registry = container.get("runtime.model_registry")
    models = registry.list_all()

    rejected: list[str] = []
    ollama_down = False
    try:
        from velune.providers.keystore import (
            KeyState,
            is_ollama_live,
            list_invalid_providers,
            verification_state,
        )

        if models:
            provider_ids = {m.provider_id for m in models}
            invalid = {p for p in provider_ids if verification_state(p) is KeyState.INVALID}
            if invalid:
                usable = [m for m in models if m.provider_id not in invalid]
                if not usable:
                    rejected = sorted(invalid)
                models = usable
        else:
            # No models at all: if that's because every configured key was
            # rejected, "run a model scan" is the wrong advice — name the fix.
            rejected = sorted(list_invalid_providers())

        # Ollama manifest models are deliberately listed even when the daemon
        # is down (they exist on disk) — but they can't answer anything. If
        # they are all that's left, block with the one-command fix instead of
        # letting the council fail five connection attempts.
        if models and all(m.provider_id == "ollama" for m in models) and not is_ollama_live():
            ollama_down = True
            models = []
    except Exception:
        pass  # key-state / liveness lookup must never block a run on its own

    if rejected:
        fixes = "\n".join(
            f"       [bold green]velune provider add {pid}[/bold green]" for pid in rejected
        )
        issues.append(
            f"The API key for [bold]{', '.join(rejected)}[/bold] was rejected by the provider.\n"
            "  [bold white]Fix:[/bold white] Save a fresh key:\n" + fixes
        )
    if ollama_down:
        issues.append(
            "Ollama models are installed, but the Ollama service isn't running.\n"
            "  [bold white]Fix:[/bold white] Start it and retry:\n"
            "       [bold green]ollama serve[/bold green]"
        )
    if not models and not rejected and not ollama_down:
        issues.append(_no_models_issue())

    if issues:
        if console:
            console.print()

            body_elements = [
                "[bold red]Velune can't start this task until the following are resolved:[/bold red]\n"
            ]
            for i, issue in enumerate(issues, 1):
                body_elements.append(f"\n[bold red]{i}.[/bold red] {issue}\n")

            body_elements.append(
                "\n[dim]Run [bold]velune doctor check[/bold] any time for a full diagnosis.[/dim]"
            )

            panel_content = Text.from_markup("".join(body_elements))

            console.print(
                Panel(
                    panel_content,
                    title="[bold red]Setup Required[/bold red]",
                    border_style="red",
                    box=ROUNDED,
                    padding=(1, 2),
                )
            )
            console.print()
        return False

    return True
