"""Interactive provider setup wizard — stores keys in the OS keychain."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from velune.cli import design
from velune.providers.keystore import get_key, has_key, save_key

console = Console()

PROVIDER_METADATA: dict[str, dict] = {
    "ollama": {
        "label": "Ollama (local — free, no key needed)",
        "requires_key": False,
        "free": True,
        "url": "https://ollama.com",
    },
    "groq": {
        "label": "Groq (cloud — free tier, very fast)",
        "requires_key": True,
        "free": True,
        "key_label": "Groq API key",
        "get_key_url": "https://console.groq.com/keys",
    },
    "openai": {
        "label": "OpenAI (cloud — GPT-4o, paid)",
        "requires_key": True,
        "free": False,
        "key_label": "OpenAI API key",
        "get_key_url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic (cloud — Claude, paid)",
        "requires_key": True,
        "free": False,
        "key_label": "Anthropic API key",
        "get_key_url": "https://console.anthropic.com",
    },
    "xai": {
        "label": "xAI (cloud — Grok, paid)",
        "requires_key": True,
        "free": False,
        "key_label": "xAI API key",
        "get_key_url": "https://console.x.ai",
    },
    "google": {
        "label": "Google Gemini (cloud — 2.0 Flash free quota)",
        "requires_key": True,
        "free": True,
        "key_label": "Google API key",
        "get_key_url": "https://aistudio.google.com/app/apikey",
    },
    "openrouter": {
        "label": "OpenRouter (cloud — one key, 100+ models)",
        "requires_key": True,
        "free": False,
        "key_label": "OpenRouter API key",
        "get_key_url": "https://openrouter.ai/keys",
    },
    "together": {
        "label": "Together.AI (cloud — Llama, Qwen, DeepSeek, cheap)",
        "requires_key": True,
        "free": False,
        "key_label": "Together.AI API key",
        "get_key_url": "https://api.together.ai/settings/api-keys",
    },
    "fireworks": {
        "label": "Fireworks.AI (cloud — fastest open-model inference)",
        "requires_key": True,
        "free": False,
        "key_label": "Fireworks.AI API key",
        "get_key_url": "https://fireworks.ai/account/api-keys",
    },
}


def run_setup_wizard() -> None:
    """Run the interactive API key setup wizard."""
    console.print(
        Panel(
            f"[bold {design.ACCENT}]Velune Provider Setup[/bold {design.ACCENT}]\n"
            f"[{design.MUTED}]Configure which AI providers you want to use.[/{design.MUTED}]\n"
            f"[{design.MUTED}]Keys are stored securely in your OS keychain.[/{design.MUTED}]\n\n"
            f"🔒 [bold {design.OK}]Privacy Notice:[/bold {design.OK}] "
            f"[{design.MUTED}]Your code and conversations stay on this machine "
            f"unless you configure a cloud provider.[/{design.MUTED}]",
            border_style=design.ACCENT,
            padding=(0, 1),
        )
    )
    console.print()

    chosen = _select_providers()
    if not chosen:
        console.print(
            f"[{design.WARN}]No providers selected. Run `velune setup` any time.[/{design.WARN}]"
        )
        return

    needs_key = [pid for pid in chosen if PROVIDER_METADATA[pid].get("requires_key")]
    no_key = [pid for pid in chosen if not PROVIDER_METADATA[pid].get("requires_key")]
    total_steps = len(needs_key)
    step = 0

    configured: list[str] = []

    for pid in no_key:
        meta = PROVIDER_METADATA[pid]
        console.print(f"[{design.OK}]✓[/{design.OK}] {meta['label']} — no key required")
        configured.append(pid)

    for pid in needs_key:
        meta = PROVIDER_METADATA[pid]
        step += 1
        step_label = f"[{design.MUTED}]({step}/{total_steps})[/{design.MUTED}]"

        console.print(f"\n{step_label} [{design.INFO}]{meta['label']}[/{design.INFO}]")
        if meta.get("get_key_url"):
            console.print(f"  [{design.MUTED}]Get your key: {meta['get_key_url']}[/{design.MUTED}]")

        if has_key(pid):
            existing = get_key(pid)
            masked = _mask_key(existing)
            overwrite = Confirm.ask(
                f"  Key already configured ({masked}). Replace it?",
                default=False,
            )
            if not overwrite:
                configured.append(pid)
                continue

        key = Prompt.ask(
            f"  Enter {meta['key_label']}",
            password=True,
        )

        if not key.strip():
            console.print(f"  [{design.WARN}]Skipped — no key entered.[/{design.WARN}]")
            continue

        save_key(pid, key.strip())
        console.print(f"  [{design.OK}]✓ Saved to OS keychain[/{design.OK}]")
        configured.append(pid)

    console.print()
    if configured:
        console.print(
            f"[bold {design.OK}]✓ Configured providers:[/bold {design.OK}] {', '.join(configured)}"
        )
        console.print(
            f"[{design.MUTED}]Run `velune doctor` to verify connectivity.[/{design.MUTED}]"
        )


def _select_providers() -> list[str]:
    table = Table(border_style=design.FAINT, padding=(0, 1))
    table.add_column("#", style=design.MUTED, width=3)
    table.add_column("Provider", style=design.INFO)
    table.add_column("Type", style=design.MUTED)
    table.add_column("Cost", style=design.MUTED)
    table.add_column("Status")

    provider_ids = list(PROVIDER_METADATA.keys())
    for i, pid in enumerate(provider_ids, 1):
        meta = PROVIDER_METADATA[pid]
        cost = (
            f"[{design.OK}]free[/{design.OK}]"
            if meta.get("free")
            else f"[{design.MUTED}]paid[/{design.MUTED}]"
        )
        status = (
            f"[{design.OK}]✓ configured[/{design.OK}]"
            if has_key(pid)
            else f"[{design.MUTED}]not set[/{design.MUTED}]"
        )
        ptype = "local" if not meta.get("requires_key") else "cloud"
        table.add_row(str(i), meta["label"], ptype, cost, status)

    console.print(table)
    console.print(
        f"  [{design.MUTED}]Recommended start: Ollama (local, free) + Groq (cloud, free tier)[/{design.MUTED}]"
    )
    console.print()

    raw = Prompt.ask(
        f"Select providers to configure [{design.MUTED}](comma-separated numbers, e.g. 1,2,3)[/{design.MUTED}]",
        default="1",
    )

    selected: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(provider_ids):
                selected.append(provider_ids[idx])
    return selected


def _mask_key(key: str | None) -> str:
    if not key:
        return "***"
    if len(key) <= 12:
        return "*" * len(key)
    return key[:8] + "..." + key[-4:]


def setup_command() -> None:
    """Configure AI provider API keys."""
    run_setup_wizard()
