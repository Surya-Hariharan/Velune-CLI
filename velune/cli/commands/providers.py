"""Provider management commands — add, remove, test, list, models, status."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from velune.cli import design
from velune.providers.keystore import (
    delete_key,
    get_key,
    has_key,
    is_ollama_live,
    save_key,
)
from velune.providers.validation import (
    ValidationStatus,
    validate_provider_sync,
)

provider_cmd = typer.Typer(
    name="provider",
    help="Manage AI provider API keys, validate credentials, and inspect available models.",
    no_args_is_help=True,
)

console = Console()

# ---------------------------------------------------------------------------
# Provider metadata (label, key_url, env_var, local flag)
# ---------------------------------------------------------------------------

_PROVIDER_META: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "env": "OPENAI_API_KEY",
        "local": False,
        "url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic",
        "env": "ANTHROPIC_API_KEY",
        "local": False,
        "url": "https://console.anthropic.com",
    },
    "google": {
        "label": "Google Gemini",
        "env": "GOOGLE_API_KEY",
        "local": False,
        "url": "https://aistudio.google.com/app/apikey",
    },
    "groq": {
        "label": "Groq",
        "env": "GROQ_API_KEY",
        "local": False,
        "url": "https://console.groq.com/keys",
    },
    "openrouter": {
        "label": "OpenRouter",
        "env": "OPENROUTER_API_KEY",
        "local": False,
        "url": "https://openrouter.ai/keys",
    },
    "deepseek": {
        "label": "DeepSeek",
        "env": "DEEPSEEK_API_KEY",
        "local": False,
        "url": "https://platform.deepseek.com/api_keys",
    },
    "mistral": {
        "label": "Mistral AI",
        "env": "MISTRAL_API_KEY",
        "local": False,
        "url": "https://console.mistral.ai/api-keys",
    },
    "cohere": {
        "label": "Cohere",
        "env": "COHERE_API_KEY",
        "local": False,
        "url": "https://dashboard.cohere.com/api-keys",
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "env": "NVIDIA_API_KEY",
        "local": False,
        "url": "https://build.nvidia.com/",
    },
    "together": {
        "label": "Together.AI",
        "env": "TOGETHER_API_KEY",
        "local": False,
        "url": "https://api.together.ai/settings/api-keys",
    },
    "fireworks": {
        "label": "Fireworks.AI",
        "env": "FIREWORKS_API_KEY",
        "local": False,
        "url": "https://fireworks.ai/account/api-keys",
    },
    "xai": {
        "label": "xAI (Grok)",
        "env": "XAI_API_KEY",
        "local": False,
        "url": "https://console.x.ai",
    },
    "huggingface": {
        "label": "HuggingFace",
        "env": "HF_TOKEN",
        "local": False,
        "url": "https://huggingface.co/settings/tokens",
    },
    "ollama": {"label": "Ollama (local)", "env": None, "local": True, "url": "https://ollama.com"},
    "lmstudio": {
        "label": "LM Studio (local)",
        "env": None,
        "local": True,
        "url": "https://lmstudio.ai",
    },
}


def _is_configured(pid: str) -> bool:
    meta = _PROVIDER_META.get(pid, {})
    if meta.get("local"):
        if pid == "ollama":
            return is_ollama_live(timeout=1.0)
        if pid == "lmstudio":
            try:
                import httpx

                r = httpx.get("http://localhost:1234/v1/models", timeout=1.0)
                return r.status_code == 200
            except Exception:
                return False
    return has_key(pid)


# ---------------------------------------------------------------------------
# velune provider list
# ---------------------------------------------------------------------------


@provider_cmd.command("list")
def list_providers() -> None:
    """List all providers and their configuration status."""
    table = Table(
        title=f"[bold {design.ACCENT}]Configured Providers[/bold {design.ACCENT}]",
        border_style=design.FAINT,
        padding=(0, 1),
    )
    table.add_column("Provider", style=design.INFO, min_width=16)
    table.add_column("Label", style=design.MUTED, min_width=20)
    table.add_column("Type", style=design.MUTED, width=7)
    table.add_column("Status", min_width=16)
    table.add_column("Env Var", style=design.MUTED)

    for pid in sorted(_PROVIDER_META.keys()):
        meta = _PROVIDER_META[pid]
        configured = _is_configured(pid)

        if meta.get("local"):
            status_str = (
                f"[{design.OK}]running[/{design.OK}]"
                if configured
                else f"[{design.WARN}]not running[/{design.WARN}]"
            )
            ptype = "local"
            env_str = "—"
        else:
            status_str = (
                f"[{design.OK}]key set[/{design.OK}]"
                if configured
                else f"[{design.MUTED}]not set[/{design.MUTED}]"
            )
            ptype = "cloud"
            env_str = meta.get("env") or "—"

        table.add_row(pid, meta["label"], ptype, status_str, env_str)

    console.print(table)
    console.print(
        f"\n[{design.MUTED}]Run `velune provider add <name>` to configure a provider.[/{design.MUTED}]"
    )


# ---------------------------------------------------------------------------
# velune provider add
# ---------------------------------------------------------------------------


@provider_cmd.command("add")
def add_provider(
    provider_id: str = typer.Argument(..., help="Provider name (e.g. openai, anthropic, groq)"),
    api_key: str = typer.Option("", "--key", "-k", help="API key (omit to be prompted)"),
    no_validate: bool = typer.Option(False, "--no-validate", help="Skip live API validation"),
) -> None:
    """Add or update a provider API key."""
    from rich.prompt import Prompt

    pid = provider_id.lower().strip()
    meta = _PROVIDER_META.get(pid)

    if not meta:
        supported = ", ".join(sorted(_PROVIDER_META.keys()))
        console.print(
            f"[{design.WARN}]Unknown provider '{pid}'.[/{design.WARN}]\n"
            f"[{design.MUTED}]Supported: {supported}[/{design.MUTED}]"
        )
        raise typer.Exit(1)

    if meta.get("local"):
        console.print(
            f"[{design.INFO}]{meta['label']} is a local provider — no API key required.[/{design.INFO}]"
        )
        with console.status(f"[{design.MUTED}]Checking {pid} server...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, "")
        if result.ok:
            console.print(f"[{design.OK}]{result.human_message()}[/{design.OK}]")
        else:
            console.print(f"[{design.WARN}]{result.human_message()}[/{design.WARN}]")
        return

    if meta.get("url"):
        console.print(f"[{design.MUTED}]Get your key at: {meta['url']}[/{design.MUTED}]")

    if not api_key:
        api_key = Prompt.ask(f"  Enter {meta['label']} API key", password=True)

    api_key = api_key.strip()
    if not api_key:
        console.print(f"[{design.WARN}]No key entered. Aborted.[/{design.WARN}]")
        raise typer.Exit(1)

    if no_validate:
        save_key(pid, api_key)
        console.print(
            f"[{design.WARN}]Key saved without validation (--no-validate was set).[/{design.WARN}]"
        )
        return

    with console.status(
        f"[{design.MUTED}]Validating {meta['label']} credentials...[/{design.MUTED}]"
    ):
        result = validate_provider_sync(pid, api_key)

    if result.ok:
        save_key(pid, api_key)
        console.print(f"[{design.OK}]{result.human_message()}[/{design.OK}]")
        if result.models:
            _show_models_preview(console, result.models[:8], len(result.models))
        if result.account_info:
            _show_account_info(console, result.account_info, pid)
    else:
        console.print(f"[{design.WARN}]{result.human_message()}[/{design.WARN}]")
        if result.status == ValidationStatus.NETWORK_ERROR:
            save_q = typer.confirm("Save key anyway (network may be offline)?", default=True)
            if save_q:
                save_key(pid, api_key)
                console.print(f"[{design.WARN}]Key saved without validation.[/{design.WARN}]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# velune provider remove
# ---------------------------------------------------------------------------


@provider_cmd.command("remove")
def remove_provider(
    provider_id: str = typer.Argument(..., help="Provider name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove a stored provider API key."""
    pid = provider_id.lower().strip()
    meta = _PROVIDER_META.get(pid)

    if meta and meta.get("local"):
        console.print(
            f"[{design.WARN}]{meta['label']} is a local provider with no stored key.[/{design.WARN}]"
        )
        return

    if not has_key(pid):
        console.print(f"[{design.MUTED}]No key stored for '{pid}'.[/{design.MUTED}]")
        return

    if not yes:
        confirmed = typer.confirm(f"Remove stored key for '{pid}'?", default=False)
        if not confirmed:
            console.print(f"[{design.MUTED}]Aborted.[/{design.MUTED}]")
            return

    delete_key(pid)
    console.print(f"[{design.OK}]Key for '{pid}' removed from OS keychain.[/{design.OK}]")


# ---------------------------------------------------------------------------
# velune provider test
# ---------------------------------------------------------------------------


@provider_cmd.command("test")
def test_provider(
    provider_id: str = typer.Argument(
        ..., help="Provider name, or 'all' to test every configured provider"
    ),
) -> None:
    """Test provider credentials with a live API round-trip."""
    if provider_id.lower() == "all":
        _test_all()
        return

    pid = provider_id.lower().strip()
    meta = _PROVIDER_META.get(pid)

    if not meta:
        console.print(f"[{design.WARN}]Unknown provider '{pid}'.[/{design.WARN}]")
        raise typer.Exit(1)

    key = "" if meta.get("local") else (get_key(pid) or "")
    if not meta.get("local") and not key:
        console.print(
            f"[{design.WARN}]No key configured for '{pid}'. Run `velune provider add {pid}` first.[/{design.WARN}]"
        )
        raise typer.Exit(1)

    with console.status(f"[{design.MUTED}]Testing {meta['label']}...[/{design.MUTED}]"):
        result = validate_provider_sync(pid, key)

    if result.ok:
        console.print(f"[{design.OK}]{result.human_message()}[/{design.OK}]")
        if result.models:
            _show_models_preview(console, result.models[:10], len(result.models))
        if result.account_info:
            _show_account_info(console, result.account_info, pid)
    else:
        console.print(f"[{design.WARN}]{result.human_message()}[/{design.WARN}]")
        raise typer.Exit(1)


def _test_all() -> None:
    table = Table(border_style=design.FAINT, padding=(0, 1))
    table.add_column("Provider", style=design.INFO, min_width=14)
    table.add_column("Status", min_width=20)
    table.add_column("Models", style=design.MUTED)
    table.add_column("Message", style=design.MUTED)

    providers_to_test = [
        (pid, meta) for pid, meta in _PROVIDER_META.items() if meta.get("local") or has_key(pid)
    ]

    if not providers_to_test:
        console.print(
            f"[{design.WARN}]No providers configured. Run `velune setup` first.[/{design.WARN}]"
        )
        return

    console.print(
        f"[{design.MUTED}]Testing {len(providers_to_test)} configured provider(s)...[/{design.MUTED}]\n"
    )

    for pid, meta in providers_to_test:
        key = "" if meta.get("local") else (get_key(pid) or "")
        with console.status(f"  [{design.MUTED}]{meta['label']}...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, key)

        if result.ok:
            status_str = f"[{design.OK}]Healthy[/{design.OK}]"
            model_str = str(len(result.models))
            msg_str = "OK"
        else:
            status_str = f"[{design.WARN}]{result.status.value.replace('_', ' ').title()}[/{design.WARN}]"
            model_str = "—"
            msg_str = result.message[:50]

        table.add_row(pid, status_str, model_str, msg_str)

    console.print(table)


# ---------------------------------------------------------------------------
# velune provider models
# ---------------------------------------------------------------------------


@provider_cmd.command("models")
def list_provider_models(
    provider_id: str = typer.Argument(..., help="Provider name"),
) -> None:
    """List models available from a provider."""
    pid = provider_id.lower().strip()
    meta = _PROVIDER_META.get(pid)

    if not meta:
        console.print(f"[{design.WARN}]Unknown provider '{pid}'.[/{design.WARN}]")
        raise typer.Exit(1)

    key = "" if meta.get("local") else (get_key(pid) or "")
    if not meta.get("local") and not key:
        console.print(
            f"[{design.WARN}]No key configured for '{pid}'. Run `velune provider add {pid}` first.[/{design.WARN}]"
        )
        raise typer.Exit(1)

    with console.status(
        f"[{design.MUTED}]Fetching models from {meta['label']}...[/{design.MUTED}]"
    ):
        result = validate_provider_sync(pid, key)

    if not result.ok:
        console.print(f"[{design.WARN}]{result.human_message()}[/{design.WARN}]")
        raise typer.Exit(1)

    if not result.models:
        console.print(f"[{design.MUTED}]No models listed for {pid}.[/{design.MUTED}]")
        return

    table = Table(
        title=f"[bold {design.ACCENT}]{meta['label']} Models[/bold {design.ACCENT}]",
        border_style=design.FAINT,
        padding=(0, 1),
    )
    table.add_column("#", style=design.MUTED, width=4)
    table.add_column("Model ID", style=design.INFO)

    for i, model_id in enumerate(result.models, 1):
        table.add_row(str(i), model_id)

    console.print(table)


# ---------------------------------------------------------------------------
# velune provider status
# ---------------------------------------------------------------------------


@provider_cmd.command("status")
def provider_status(
    provider_id: str = typer.Argument(
        "", help="Provider name, or leave empty for all configured providers"
    ),
) -> None:
    """Show real-time health status of one or all configured providers."""
    if provider_id:
        pids = [provider_id.lower().strip()]
    else:
        pids = [pid for pid, meta in _PROVIDER_META.items() if meta.get("local") or has_key(pid)]

    if not pids:
        console.print(f"[{design.WARN}]No providers configured.[/{design.WARN}]")
        return

    table = Table(
        title=f"[bold {design.ACCENT}]Provider Health Status[/bold {design.ACCENT}]",
        border_style=design.FAINT,
        padding=(0, 1),
    )
    table.add_column("Provider", style=design.INFO, min_width=14)
    table.add_column("Status", min_width=18)
    table.add_column("Models", style=design.MUTED, width=8)
    table.add_column("Message", style=design.MUTED)

    for pid in sorted(pids):
        meta = _PROVIDER_META.get(pid, {"label": pid, "local": False})
        key = "" if meta.get("local") else (get_key(pid) or "")

        with console.status(f"  [{design.MUTED}]Checking {pid}...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, key)

        if result.ok:
            status_str = f"[{design.OK}]Healthy[/{design.OK}]"
            model_count = str(len(result.models)) if result.models else "—"
            msg = "Authenticated"
        else:
            color = design.WARN
            status_str = (
                f"[{color}]{result.status.value.replace('_', ' ').title()}[/{color}]"
            )
            model_count = "—"
            msg = result.message[:60]

        table.add_row(pid, status_str, model_count, msg)

    console.print(table)


# ---------------------------------------------------------------------------
# velune provider edit
# ---------------------------------------------------------------------------


@provider_cmd.command("edit")
def edit_provider(
    provider_id: str = typer.Argument(..., help="Provider name"),
) -> None:
    """Update an existing provider API key."""
    # edit is just an alias for add that always prompts
    add_provider(provider_id=provider_id, api_key="", no_validate=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _show_models_preview(c: Console, models: list[str], total: int) -> None:
    preview = ", ".join(models)
    if total > len(models):
        preview += f" +{total - len(models)} more"
    c.print(f"  [{design.MUTED}]Models: {preview}[/{design.MUTED}]")


def _show_account_info(c: Console, info: dict, pid: str) -> None:
    parts = []
    if "username" in info:
        parts.append(f"user: {info['username']}")
    if "organization" in info:
        parts.append(f"org: {info['organization']}")
    if "model_count" in info:
        parts.append(f"{info['model_count']} models")
    if "total_models" in info:
        parts.append(f"{info['total_models']} total models")
    if parts:
        c.print(f"  [{design.MUTED}]Account: {', '.join(parts)}[/{design.MUTED}]")
