"""Provider management commands — add, remove, test, list, models, status."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from velune.cli import design
from velune.providers import catalog
from velune.providers.credential_manager import add_credential_sync
from velune.providers.crypto import encrypt_credentials
from velune.providers.default_provider import (
    find_config_path,
    get_default_provider,
    set_default_provider,
    set_first_default,
)
from velune.providers.keystore import (
    credentials_file_path,
    delete_key,
    export_providers_json,
    get_key,
    get_provider_status,
    has_key,
    import_providers_json,
    is_ollama_live,
    repair_keystore,
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
# Provider metadata
#
# Sourced from velune.providers.catalog — the single place provider display
# metadata is maintained (also used by the REPL's /providers and the
# onboarding wizard). This module used to hand-maintain its own, separate
# {label, env, local, url} table for the same providers; ``_meta_dict``
# below is only a shape-adapter so the rest of this file (written against
# that dict shape) doesn't need to change, not a second data source.
# ---------------------------------------------------------------------------


def _meta_dict(pid: str) -> dict | None:
    """Adapt a ``catalog.ProviderMeta`` to this module's ``{label, env, local,
    url}`` shape."""
    meta = catalog.get(pid)
    if meta is None:
        return None
    return {
        "label": meta.display_name,
        "env": meta.env_var,
        "local": not meta.requires_key,
        "url": meta.get_key_url,
    }


def _all_provider_ids() -> list[str]:
    return sorted(p.id for p in catalog.list_providers_alphabetical())


def _is_configured(pid: str) -> bool:
    meta = _meta_dict(pid) or {}
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
    default_pid = get_default_provider()

    table = Table(
        title=f"[bold {design.ACCENT}]Configured Providers[/bold {design.ACCENT}]",
        border_style=design.FAINT,
        padding=(0, 1),
    )
    table.add_column(" ", width=2)  # default marker
    table.add_column("Provider", style=design.INFO, min_width=14)
    table.add_column("Label", style=design.MUTED, min_width=20)
    table.add_column("Type", style=design.MUTED, width=7)
    table.add_column("Status", min_width=16)
    table.add_column("Source", style=design.MUTED, width=5)

    for pid in _all_provider_ids():
        meta = _meta_dict(pid)
        if meta is None:
            continue
        configured = _is_configured(pid)
        is_default = pid == default_pid
        marker = f"[{design.OK}]★[/{design.OK}]" if is_default else ""

        if meta.get("local"):
            status_str = (
                f"[{design.OK}]running[/{design.OK}]"
                if configured
                else f"[{design.WARN}]not running[/{design.WARN}]"
            )
            ptype = "local"
            source_str = "—"
        else:
            if configured:
                status_str = f"[{design.OK}]key set[/{design.OK}]"
                info = get_provider_status(pid)
                source_str = "env" if info.get("source") == "environment" else "file"
            else:
                status_str = f"[{design.MUTED}]not set[/{design.MUTED}]"
                source_str = "—"
            ptype = "cloud"

        table.add_row(marker, pid, meta["label"], ptype, status_str, source_str)

    console.print(table)
    if default_pid:
        console.print(
            f"\n[{design.MUTED}]★ default provider: [bold]{default_pid}[/bold]"
            f"  ·  Run `velune provider default <name>` to change.[/{design.MUTED}]"
        )
    else:
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

    from velune.providers.credential_manager import persist_credential

    pid = provider_id.lower().strip()
    meta = _meta_dict(pid)

    if not meta:
        supported = ", ".join(_all_provider_ids())
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
            if set_first_default(pid):
                console.print(
                    f"[{design.OK}]★ Set as default provider (first configured).[/{design.OK}]"
                )
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
        added = add_credential_sync(pid, api_key, skip_validation=True, set_as_first_default=True)
        console.print(
            f"[{design.WARN}]Key saved without validation (--no-validate was set).[/{design.WARN}]"
        )
        if added.became_default:
            console.print(
                f"[{design.OK}]★ Set as default provider (first configured).[/{design.OK}]"
            )
        return

    with console.status(
        f"[{design.MUTED}]Validating {meta['label']} credentials...[/{design.MUTED}]"
    ):
        added = add_credential_sync(pid, api_key, set_as_first_default=True)
    result = added.validation

    if added.ok:
        console.print(f"[{design.OK}]{result.human_message()}[/{design.OK}]")
        if result.models:
            _show_models_preview(console, result.models[:8], len(result.models))
        if result.account_info:
            _show_account_info(console, result.account_info, pid)
        if added.became_default:
            console.print(
                f"[{design.OK}]★ Set as default provider (first configured).[/{design.OK}]"
            )
        _print_provider_next_steps(pid, result.models, validated=True)
    else:
        console.print(f"[{design.WARN}]{result.human_message()}[/{design.WARN}]")
        if result.status == ValidationStatus.NETWORK_ERROR:
            save_q = typer.confirm("Save key anyway (network may be offline)?", default=True)
            if save_q:
                became_default = persist_credential(
                    pid, api_key, verified=False, set_as_first_default=True
                )
                console.print(f"[{design.WARN}]Key saved without validation.[/{design.WARN}]")
                if became_default:
                    console.print(
                        f"[{design.OK}]★ Set as default provider (first configured).[/{design.OK}]"
                    )
                _print_provider_next_steps(pid, result.models, validated=False)
        raise typer.Exit(1)


def _print_provider_next_steps(pid: str, models: list[str], *, validated: bool) -> None:
    """Close the add-provider flow with concrete follow-ups (Do → Suggest → Next)."""
    from velune.cli import guidance, ui

    suggested_model = models[0] if models else "<model-id>"
    outcome = "provider_added" if validated else "provider_added_unvalidated"
    steps = guidance.steps_for(outcome, model=suggested_model)
    if not steps:
        return
    summary = (
        f"{pid} validated — {len(models)} model(s) available."
        if validated
        else f"{pid} key saved (not yet validated)."
    )
    console.print(
        ui.next_steps(
            "Provider added",
            summary,
            steps,
            kind="success" if validated else "warning",
        )
    )


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
    meta = _meta_dict(pid)

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
    meta = _meta_dict(pid)

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
        (pid, meta)
        for pid in _all_provider_ids()
        if (meta := _meta_dict(pid)) is not None and (meta.get("local") or has_key(pid))
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
            status_str = (
                f"[{design.WARN}]{result.status.value.replace('_', ' ').title()}[/{design.WARN}]"
            )
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
    meta = _meta_dict(pid)

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
        pids = [
            pid
            for pid in _all_provider_ids()
            if (_meta_dict(pid) or {}).get("local") or has_key(pid)
        ]

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
        meta = _meta_dict(pid) or {"label": pid, "local": False}
        key = "" if meta.get("local") else (get_key(pid) or "")

        with console.status(f"  [{design.MUTED}]Checking {pid}...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, key)

        if result.ok:
            status_str = f"[{design.OK}]Healthy[/{design.OK}]"
            model_count = str(len(result.models)) if result.models else "—"
            msg = "Authenticated"
        else:
            color = design.WARN
            status_str = f"[{color}]{result.status.value.replace('_', ' ').title()}[/{color}]"
            model_count = "—"
            msg = result.message[:60]

        table.add_row(pid, status_str, model_count, msg)

    console.print(table)


# ---------------------------------------------------------------------------
# velune provider api
# ---------------------------------------------------------------------------


@provider_cmd.command("api")
def api_status(
    provider_id: str = typer.Argument(
        "", help="Provider name, or leave empty for all configured providers"
    ),
) -> None:
    """Show detailed internal diagnostic status for provider API keys."""
    if provider_id:
        pids = [provider_id.lower().strip()]
    else:
        pids = [pid for pid in _all_provider_ids() if not (_meta_dict(pid) or {}).get("local")]

    for pid in sorted(pids):
        info = get_provider_status(pid)
        key = get_key(pid) or ""

        console.print(f"\n[bold {design.ACCENT}]{pid.capitalize()}[/bold {design.ACCENT}]")

        # Stored check
        if info["stored"]:
            console.print(
                f"[{design.OK}]✓ Stored[/{design.OK}] [{design.MUTED}]({info['location']})[/{design.MUTED}]"
            )
        else:
            console.print(f"[{design.WARN}]✗ Not stored[/{design.WARN}]")
            continue

        # Validation Check (Live)
        with console.status(f"  [{design.MUTED}]Validating...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, key)

        if result.ok:
            console.print(f"[{design.OK}]✓ Valid[/{design.OK}]")
            console.print(f"[{design.OK}]✓ Reachable[/{design.OK}]")
            console.print(f"[{design.OK}]✓ Loaded[/{design.OK}]")
        else:
            if result.status == ValidationStatus.NETWORK_ERROR:
                console.print(f"[{design.WARN}]⚠ Offline validation unavailable[/{design.WARN}]")
            else:
                console.print(
                    f"[{design.DANGER}]✗ Invalid[/{design.DANGER}] [{design.MUTED}]({result.message})[/{design.MUTED}]"
                )


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
# velune provider inspect
# ---------------------------------------------------------------------------


@provider_cmd.command("inspect")
def inspect_provider(
    provider_id: str = typer.Argument(..., help="Provider name"),
    no_live: bool = typer.Option(False, "--no-live", help="Skip live API validation"),
) -> None:
    """Show comprehensive details for a single provider."""
    pid = provider_id.lower().strip()
    meta = _meta_dict(pid)

    if not meta:
        supported = ", ".join(_all_provider_ids())
        console.print(
            f"[{design.WARN}]Unknown provider '{pid}'.[/{design.WARN}]\n"
            f"[{design.MUTED}]Supported: {supported}[/{design.MUTED}]"
        )
        raise typer.Exit(1)

    info = get_provider_status(pid)
    default_pid = get_default_provider()
    is_local = bool(meta.get("local"))
    key = "" if is_local else (get_key(pid) or "")

    lines: list[str] = []

    # Identity
    lines.append(f"[{design.MUTED}]Label[/{design.MUTED}]          {meta['label']}")
    lines.append(
        f"[{design.MUTED}]Type[/{design.MUTED}]           {'local' if is_local else 'cloud'}"
    )

    if pid == default_pid:
        lines.append(
            f"[{design.MUTED}]Default[/{design.MUTED}]        [{design.OK}]★ yes[/{design.OK}]"
        )

    # Key / auth
    if is_local:
        lines.append(f"[{design.MUTED}]Auth[/{design.MUTED}]           no key required")
    elif key:
        # No fragment of the real key, ever — this output can end up pasted
        # into a shared terminal, a bug report, or a screen recording, and
        # even a partial key materially narrows a brute-force search.
        masked = "•" * 20
        source_label = info.get("source", "file")
        if source_label == "environment":
            env_var = meta.get("env", "")
            lines.append(
                f"[{design.MUTED}]API Key[/{design.MUTED}]        {masked}"
                f"  [{design.MUTED}](${env_var})[/{design.MUTED}]"
            )
        else:
            lines.append(f"[{design.MUTED}]API Key[/{design.MUTED}]        {masked}  (file)")
    else:
        lines.append(
            f"[{design.MUTED}]API Key[/{design.MUTED}]        [{design.WARN}]not configured[/{design.WARN}]"
        )

    # Storage
    if not is_local:
        loc = info.get("location", "—")
        lines.append(f"[{design.MUTED}]Storage[/{design.MUTED}]        {loc}")

    # Last verified
    lv = info.get("last_verified", "n/a")
    if lv and lv not in ("n/a", "dynamic"):
        try:
            dt = datetime.fromisoformat(lv)
            lv = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass
    lines.append(f"[{design.MUTED}]Last Verified[/{design.MUTED}]  {lv}")

    # Env var
    if meta.get("env"):
        env_set = "set" if get_provider_status(pid).get("source") == "environment" else "not set"
        lines.append(
            f"[{design.MUTED}]Env Var[/{design.MUTED}]        {meta['env']}"
            f"  [{design.MUTED}]({env_set})[/{design.MUTED}]"
        )

    # URL
    if meta.get("url"):
        lines.append(f"[{design.MUTED}]Key URL[/{design.MUTED}]        {meta['url']}")

    lines.append("")

    # Live status
    if no_live:
        lines.append(
            f"[{design.MUTED}](live check skipped — pass without --no-live to validate)[/{design.MUTED}]"
        )
    elif not key and not is_local:
        lines.append(f"[{design.WARN}]Live check skipped: no key configured.[/{design.WARN}]")
    else:
        t0 = time.monotonic()
        with console.status(f"  [{design.MUTED}]Checking {meta['label']}...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, key)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if result.ok:
            lines.append(
                f"[{design.OK}]✓ Healthy[/{design.OK}]  [{design.MUTED}]{latency_ms}ms[/{design.MUTED}]"
            )
            if result.models:
                n = len(result.models)
                preview = ", ".join(result.models[:4])
                if n > 4:
                    preview += f" +{n - 4} more"
                lines.append(f"[{design.MUTED}]Models[/{design.MUTED}]         {n} available")
                lines.append(f"[{design.MUTED}]               {preview}[/{design.MUTED}]")
            if result.account_info:
                _append_account_info(lines, result.account_info)
        else:
            status_label = result.status.value.replace("_", " ").title()
            lines.append(
                f"[{design.WARN}]✗ {status_label}[/{design.WARN}]"
                f"  [{design.MUTED}]{latency_ms}ms[/{design.MUTED}]"
            )
            lines.append(f"  [{design.MUTED}]{result.message}[/{design.MUTED}]")

    body = "\n".join(lines)
    console.print(
        Panel(
            body,
            title=f"[bold {design.ACCENT}]{pid}[/bold {design.ACCENT}]",
            border_style=design.GREEN,
            padding=(0, 2),
        )
    )


# ---------------------------------------------------------------------------
# velune provider default
# ---------------------------------------------------------------------------


@provider_cmd.command("default")
def provider_default(
    provider_id: str = typer.Argument(
        "", help="Provider to set as default. Omit to show current default."
    ),
) -> None:
    """Get or set the default AI provider."""
    if not provider_id:
        current = get_default_provider()
        config_path = find_config_path()

        if current:
            console.print(f"[{design.OK}]★ Default provider:[/{design.OK}] [bold]{current}[/bold]")
            if config_path:
                console.print(f"  [{design.MUTED}]{config_path}[/{design.MUTED}]")
        else:
            console.print(
                f"[{design.WARN}]No default provider set.[/{design.WARN}]\n"
                f"[{design.MUTED}]Run `velune provider default <name>` to set one.[/{design.MUTED}]"
            )
        return

    pid = provider_id.lower().strip()
    meta = _meta_dict(pid)

    if not meta:
        supported = ", ".join(_all_provider_ids())
        console.print(
            f"[{design.WARN}]Unknown provider '{pid}'.[/{design.WARN}]\n"
            f"[{design.MUTED}]Supported: {supported}[/{design.MUTED}]"
        )
        raise typer.Exit(1)

    if not meta.get("local") and not has_key(pid):
        console.print(
            f"[{design.WARN}]'{pid}' has no configured API key.[/{design.WARN}]\n"
            f"[{design.MUTED}]Run `velune provider add {pid}` first.[/{design.MUTED}]"
        )
        raise typer.Exit(1)

    config_path = set_default_provider(pid)
    if config_path:
        console.print(
            f"[{design.OK}]★ Default provider set to:[/{design.OK}] [bold]{pid}[/bold]\n"
            f"  [{design.MUTED}]{config_path}[/{design.MUTED}]"
        )
    else:
        console.print(
            f"[{design.WARN}]Could not write to velune.toml.[/{design.WARN}]\n"
            f'[{design.MUTED}]Add `default_provider = "{pid}"` under [providers] in velune.toml manually.[/{design.MUTED}]'
        )
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# velune provider backup
# ---------------------------------------------------------------------------


@provider_cmd.command("backup")
def backup_providers(
    output: str = typer.Option(
        "", "--output", "-o", help="Output file path (default: velune-providers-YYYYMMDD.json)"
    ),
) -> None:
    """Export all configured provider credentials to a JSON backup file.

    The exported file contains plaintext API keys — keep it secure.
    """
    snapshot = export_providers_json(include_keys=True)

    if not snapshot:
        console.print(
            f"[{design.WARN}]No providers configured — nothing to back up.[/{design.WARN}]"
        )
        raise typer.Exit(1)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = Path(output) if output else Path.cwd() / f"velune-providers-{date_str}.json"

    payload = {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "providers": snapshot,
    }

    serialized = json.dumps(payload, separators=(",", ":"))
    encrypted_payload = encrypt_credentials(serialized)
    dest.write_text(encrypted_payload, encoding="utf-8")
    console.print(
        f"[{design.OK}]Encrypted backup written:[/{design.OK}] {dest}\n"
        f"[{design.MUTED}]Credentials are encrypted at rest in this backup file.[/{design.MUTED}]"
    )
    console.print(
        f"[{design.MUTED}]Providers: {', '.join(sorted(snapshot.keys()))}[/{design.MUTED}]"
    )


# ---------------------------------------------------------------------------
# velune provider restore
# ---------------------------------------------------------------------------


@provider_cmd.command("restore")
def restore_providers(
    file_path: str = typer.Argument(..., help="Path to a velune provider backup JSON file"),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Overwrite keys for providers already configured"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Import provider credentials from a backup JSON file."""
    src = Path(file_path)
    if not src.exists():
        console.print(f"[{design.WARN}]File not found: {src}[/{design.WARN}]")
        raise typer.Exit(1)

    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[{design.WARN}]Could not read backup file: {exc}[/{design.WARN}]")
        raise typer.Exit(1)

    records: dict = payload.get("providers", payload)
    if not isinstance(records, dict) or not records:
        console.print(f"[{design.WARN}]Backup file contains no provider records.[/{design.WARN}]")
        raise typer.Exit(1)

    exported_at = payload.get("exported_at", "unknown")
    console.print(
        f"[{design.MUTED}]Backup from:[/{design.MUTED}] {exported_at}\n"
        f"[{design.MUTED}]Providers in file:[/{design.MUTED}] {', '.join(sorted(records.keys()))}"
    )

    if not yes:
        confirmed = typer.confirm("Import these providers?", default=True)
        if not confirmed:
            console.print(f"[{design.MUTED}]Aborted.[/{design.MUTED}]")
            return

    imported, skipped = import_providers_json(records, overwrite=overwrite)

    if imported:
        console.print(f"[{design.OK}]Imported:[/{design.OK}] {', '.join(sorted(imported))}")
    if skipped:
        hint = " (use --overwrite to replace existing keys)" if not overwrite else ""
        console.print(
            f"[{design.MUTED}]Skipped:[/{design.MUTED}] {', '.join(sorted(skipped))}{hint}"
        )

    if not imported and not skipped:
        console.print(f"[{design.MUTED}]Nothing to import.[/{design.MUTED}]")


# ---------------------------------------------------------------------------
# velune provider repair
# ---------------------------------------------------------------------------


@provider_cmd.command("repair")
def repair_providers_cmd() -> None:
    """Attempt to repair corrupted or invalid provider credentials.

    Tries to restore from the automatic backup, then removes any records
    with empty or missing keys.
    """
    console.print(f"[{design.MUTED}]Running credential repair...[/{design.MUTED}]\n")

    report = repair_keystore()

    if report["restored_backup"]:
        console.print(f"[{design.OK}]✓ Restored from backup (.bak file)[/{design.OK}]")
    else:
        console.print(
            f"[{design.MUTED}]Primary credential store loaded (no restore needed)[/{design.MUTED}]"
        )

    if report["removed"]:
        console.print(
            f"[{design.WARN}]Removed empty records:[/{design.WARN}] {', '.join(report['removed'])}"
        )
    else:
        console.print(f"[{design.OK}]✓ No empty records found[/{design.OK}]")

    if report["kept"]:
        console.print(
            f"[{design.OK}]✓ Valid providers kept:[/{design.OK}] {', '.join(report['kept'])}"
        )

    creds = credentials_file_path()
    console.print(f"\n[{design.MUTED}]Credentials file: {creds}[/{design.MUTED}]")
    bak = creds.with_name("credentials.json.bak")
    if bak.exists():
        console.print(f"[{design.MUTED}]Auto-backup file: {bak}[/{design.MUTED}]")

    if not report["kept"] and not report["removed"]:
        console.print(
            f"\n[{design.WARN}]No providers found after repair. Run `velune setup` to add one.[/{design.WARN}]"
        )


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


def _append_account_info(lines: list[str], info: dict) -> None:
    parts = []
    if "username" in info:
        parts.append(f"user: {info['username']}")
    if "organization" in info:
        parts.append(f"org: {info['organization']}")
    if parts:
        lines.append(f"[{design.MUTED}]Account[/{design.MUTED}]        {', '.join(parts)}")
