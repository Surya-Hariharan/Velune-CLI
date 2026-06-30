"""First-run onboarding orchestrator for Velune CLI.

State machine
─────────────
  RETURNING  (providers + model both exist) → show_returning_summary → REPL
  PARTIAL    (providers exist, no model)    → model_discovery → REPL
  FRESH      (no providers at all)          → full wizard → REPL

Every Ctrl+C or EOF at any step prints a recovery hint and falls through to the
REPL (never hard-exits mid-session).  Every input validates and retries — no
silent failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from velune.cli import design

if TYPE_CHECKING:
    from velune.core.types.model import ModelDescriptor

# ── Provider catalogue shown during onboarding (7 of 15) ──────────────────────
# Free-tier providers appear first so the "no credit card" path is obvious.

_ONBOARDING_CLOUD: list[tuple[str, bool, str, str, str]] = [
    # (provider_id, is_free, display_label, key_label, get_key_url)
    (
        "groq",
        True,
        "Groq  — free tier, very fast",
        "Groq API key",
        "https://console.groq.com/keys",
    ),
    (
        "google",
        True,
        "Google Gemini  — free quota (Gemini 2.0 Flash)",
        "Google API key",
        "https://aistudio.google.com/app/apikey",
    ),
    (
        "huggingface",
        True,
        "HuggingFace  — free Inference API",
        "HuggingFace token",
        "https://huggingface.co/settings/tokens",
    ),
    (
        "anthropic",
        False,
        "Anthropic  — Claude (best reasoning, paid)",
        "Anthropic API key",
        "https://console.anthropic.com",
    ),
    (
        "openai",
        False,
        "OpenAI  — GPT-4o (paid)",
        "OpenAI API key",
        "https://platform.openai.com/api-keys",
    ),
    (
        "deepseek",
        False,
        "DeepSeek  — very cheap cloud models",
        "DeepSeek API key",
        "https://platform.deepseek.com/api_keys",
    ),
    (
        "openrouter",
        False,
        "OpenRouter  — 100+ models with one key",
        "OpenRouter API key",
        "https://openrouter.ai/keys",
    ),
]

_MAX_LOCAL_RETRIES = 3
_MAX_KEY_ATTEMPTS = 3

# ── Progress persistence ───────────────────────────────────────────────────────

_PROGRESS_FILE = Path.home() / ".velune" / "onboarding_progress.json"

_STAGE_NAMES: tuple[str, ...] = (
    "welcome",
    "detect_environment",
    "configure_providers",
    "discover_models",
    "select_default_model",
    "health_check",
    "workspace_setup",
    "ready",
)

_log = logging.getLogger("velune.cli.onboarding")


def _read_progress_file() -> dict:
    try:
        if _PROGRESS_FILE.exists():
            return json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_progress_file(data: dict) -> None:
    try:
        _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_PROGRESS_FILE)
    except Exception as exc:
        _log.warning("Could not save onboarding progress: %s", exc)


def save_stage_progress(stage: str) -> None:
    """Append *stage* to the completed-stages list in the progress file."""
    data = _read_progress_file()
    stages: list[str] = data.get("completed_stages", [])
    if stage not in stages:
        stages.append(stage)
    data["completed_stages"] = stages
    data["version"] = 1
    _write_progress_file(data)


def load_stage_progress() -> list[str]:
    """Return the list of completed stage names (empty list if not started)."""
    return _read_progress_file().get("completed_stages", [])


def mark_onboarding_complete() -> None:
    """Write the 'complete' flag so onboarding_state() returns 'returning'."""
    data = _read_progress_file()
    data["complete"] = True
    data["version"] = 1
    _write_progress_file(data)


# ── Public API ─────────────────────────────────────────────────────────────────


def onboarding_state() -> Literal["returning", "partial", "fresh"]:
    """Classify the current installation state.

    Returns
    -------
    "returning"
        Both providers and a default model are configured — skip onboarding.
    "partial"
        Providers exist but no model is selected — run abbreviated onboarding
        starting at model discovery.
    "fresh"
        No providers configured at all — run the full wizard.
    """
    # Fast-path: if the wizard completed successfully, skip all live checks.
    if _read_progress_file().get("complete"):
        return "returning"

    from velune.cli.model_prefs import load_active_model
    from velune.providers.keystore import list_configured_providers

    configured = list_configured_providers()
    model = load_active_model()

    if configured and model:
        return "returning"
    if configured:
        return "partial"
    return "fresh"


def show_returning_summary(
    console: Console,
    configured: list[str],
    model_pref: object | None,
) -> None:
    """Print the brief 'welcome back' panel for returning users."""
    lines: list[str] = []

    if model_pref:
        provider = getattr(model_pref, "provider_id", "")
        model_id = getattr(model_pref, "model_id", "")
        lines.append(
            f"  [{design.MUTED}]Model    [/{design.MUTED}]"
            f" {provider} / [bold]{model_id}[/bold]  [dim](restored)[/dim]"
        )

    if configured:
        providers_str = "  ·  ".join(p.title() for p in configured[:6])
        if len(configured) > 6:
            providers_str += f"  [dim]+{len(configured) - 6} more[/dim]"
        lines.append(f"  [{design.MUTED}]Providers[/{design.MUTED}] {providers_str}")

    if not lines:
        return

    content = f"[bold {design.ACCENT}]Welcome back.[/bold {design.ACCENT}]\n\n" + "\n".join(lines)
    console.print(Panel(content, border_style=design.GREEN, padding=(0, 2)))


def run_onboarding(
    runtime: object,
    skip_to: str | None = None,
    start_stage: int = 0,
) -> None:
    """Entry point for the onboarding wizard.

    Parameters
    ----------
    runtime:
        The ``RuntimeContext`` produced by ``build_runtime()``.
    skip_to:
        Legacy parameter — ``"model_discovery"`` maps to ``start_stage=3``.
        Prefer ``start_stage`` for new callers.
    start_stage:
        0-indexed stage to begin from (0 = Stage 1 Welcome, 7 = Stage 8 Ready).
        Stages before this index are skipped.
    """
    console: Console = runtime.console  # type: ignore[attr-defined]
    if skip_to == "model_discovery" and start_stage == 0:
        start_stage = 3
    try:
        _run_inner(runtime, start_stage)
    except KeyboardInterrupt:
        console.print()
        console.print(
            f"[{design.WARN}]Setup interrupted.[/{design.WARN}]"
            f" [{design.MUTED}]Run [bold]velune onboard[/bold] any time to resume.[/{design.MUTED}]"
        )
    except EOFError:
        pass


# ── Stage UI helpers ───────────────────────────────────────────────────────────


def _stage_header(console: Console, n: int, total: int, name: str) -> None:
    """Print a numbered stage header with a horizontal rule."""
    console.print()
    console.print(
        f"[bold {design.ACCENT}]Stage {n}/{total} — {name}[/bold {design.ACCENT}]"
    )
    console.print(f"[{design.FAINT}]{'─' * 52}[/{design.FAINT}]")


def _stage_done(console: Console, summary: str, next_hint: str) -> None:
    """Print the ✓ Done success panel that ends every stage."""
    body = (
        f"[{design.MUTED}]{summary}[/{design.MUTED}]\n\n"
        f"[{design.MUTED}]Next: {next_hint}[/{design.MUTED}]"
    )
    console.print(
        Panel(
            f"[bold {design.OK}]{design.ICON_SUCCESS}  Done[/bold {design.OK}]\n\n{body}",
            border_style=design.GREEN,
            padding=(0, 2),
        )
    )


# ── Internal orchestrator ──────────────────────────────────────────────────────


def _run_inner(runtime: object, start_stage: int = 0) -> None:
    console: Console = runtime.console  # type: ignore[attr-defined]

    try:
        workspace_val = runtime.container.get("runtime.workspace")  # type: ignore[attr-defined]
        workspace = Path(workspace_val) if workspace_val else Path.cwd()
    except Exception:
        workspace = Path.cwd()

    # ── Stage 1: Welcome ───────────────────────────────────────────────────
    mode: str = "hybrid"  # default when resuming past stage 1
    if start_stage <= 0:
        _stage_header(console, 1, 8, "Welcome")
        action = _step_welcome(console)
        if action == "skip":
            _step_degraded_mode(console)
            return
        mode = _step_ai_mode(console)
        if mode == "skip":
            _step_degraded_mode(console)
            return
        _stage_done(
            console,
            f"Mode selected: {mode}.",
            "Stage 2/8 — Detect Environment",
        )
        save_stage_progress("welcome")

    # ── Stage 2: Detect Environment ────────────────────────────────────────
    if start_stage <= 1:
        _stage_header(console, 2, 8, "Detect Environment")
        _step_detect_environment(console)
        _stage_done(
            console,
            "Hardware profile detected.",
            "Stage 3/8 — Configure Providers",
        )
        save_stage_progress("detect_environment")

    # ── Stage 3: Configure Providers ───────────────────────────────────────
    configured: list[str] = []
    preferred_local: bool = False
    if start_stage <= 2:
        _stage_header(console, 3, 8, "Configure Providers")
        if mode in ("local", "hybrid"):
            local_cfg = _step_local_detection(console)
            configured.extend(local_cfg)
        if mode in ("cloud", "hybrid"):
            cloud_cfg = _step_cloud_setup(console)
            configured.extend(cloud_cfg)
        if not configured:
            _step_degraded_mode(console)
            return
        preferred_local = mode in ("local", "hybrid")
        provider_str = ", ".join(configured) if configured else "none"
        _stage_done(
            console,
            f"{len(configured)} provider(s) configured: {provider_str}.",
            "Stage 4/8 — Discover Models",
        )
        save_stage_progress("configure_providers")
    else:
        from velune.providers.keystore import list_configured_providers

        configured = list_configured_providers()
        preferred_local = any(p in ("ollama", "lmstudio") for p in configured)

    # ── Stage 4: Discover Models ────────────────────────────────────────────
    models: list = []
    if start_stage <= 3:
        _stage_header(console, 4, 8, "Discover Models")
        models = _step_model_discovery(console)
        total_models = len(models)
        _stage_done(
            console,
            f"{total_models} model(s) found across your configured providers.",
            "Stage 5/8 — Select Default Model",
        )
        save_stage_progress("discover_models")

    # ── Stage 5: Select Default Model ──────────────────────────────────────
    if start_stage <= 4:
        _stage_header(console, 5, 8, "Select Default Model")
        if models:
            _step_model_recommendation(console, models, preferred_local)
        else:
            console.print(
                f"\n  [{design.WARN}]No models found — try [bold]/model discover[/bold]"
                f" later once a provider is reachable.[/{design.WARN}]"
            )
        from velune.cli.model_prefs import load_active_model

        model_pref = load_active_model()
        if model_pref:
            model_str = f"{model_pref.provider_id} / {model_pref.model_id}"
        else:
            model_str = "none set — use /model connect in the REPL"
        _stage_done(
            console,
            f"Default model: {model_str}.",
            "Stage 6/8 — Health Check",
        )
        save_stage_progress("select_default_model")

    # ── Stage 6: Health Check ───────────────────────────────────────────────
    if start_stage <= 5:
        _stage_header(console, 6, 8, "Health Check")
        hc_summary = _step_health_check(console)
        _stage_done(
            console,
            hc_summary,
            "Stage 7/8 — Workspace Setup",
        )
        save_stage_progress("health_check")

    # ── Stage 7: Workspace Setup ────────────────────────────────────────────
    if start_stage <= 6:
        _stage_header(console, 7, 8, "Workspace Setup")
        _step_workspace_detection(console, workspace)
        _stage_done(
            console,
            f"Workspace configured at {workspace}.",
            "Stage 8/8 — Ready",
        )
        save_stage_progress("workspace_setup")

    # ── Stage 8: Ready ──────────────────────────────────────────────────────
    _stage_header(console, 8, 8, "Ready")
    from velune.cli.model_prefs import load_active_model
    from velune.providers.keystore import list_configured_providers

    _step_ready_summary(
        console,
        list_configured_providers(),
        workspace,
        load_active_model(),
    )
    save_stage_progress("ready")
    mark_onboarding_complete()


# ── Step S1: welcome ───────────────────────────────────────────────────────────


def _step_welcome(console: Console) -> Literal["begin", "skip"]:
    """Show the welcome panel and ask the user to begin or skip setup."""
    welcome_body = (
        f"[bold {design.ACCENT}]Welcome to Velune.[/bold {design.ACCENT}]\n\n"
        f"  [{design.MUTED}]{design.ICON_BULLET}  Connects to local AI (Ollama, LM Studio)"
        f" and cloud providers (Groq, Anthropic, OpenAI, and more)[/{design.MUTED}]\n"
        f"  [{design.MUTED}]{design.ICON_BULLET}  Stores everything locally — keys in OS"
        f" keychain, data in [bold]~/.velune[/bold][/{design.MUTED}]\n"
        f"  [{design.MUTED}]{design.ICON_BULLET}  This wizard runs once, then Velune"
        f" remembers you[/{design.MUTED}]\n\n"
        f"[{design.MUTED}]Setup takes about 2 minutes. You can skip at any time.[/{design.MUTED}]"
    )
    console.print(Panel(welcome_body, border_style=design.ACCENT, padding=(1, 2)))
    console.print(
        f"\n  [{design.MUTED}]Press [bold]Enter[/bold] to begin"
        f"  {design.SEP}  [bold]S[/bold] to skip setup[/{design.MUTED}]\n"
    )
    raw = Prompt.ask("  ›", default="").strip().lower()
    if raw in ("s", "skip", "q", "quit"):
        return "skip"
    return "begin"


# ── Step S2: detect environment ────────────────────────────────────────────────


def _step_detect_environment(console: Console) -> None:
    """Probe hardware (CPU/GPU/VRAM) and display a summary table."""
    from velune.hardware.detector import HardwareDetector

    with console.status(f"  [{design.MUTED}]Scanning hardware...[/{design.MUTED}]"):
        try:
            profile = HardwareDetector().detect()
        except Exception as exc:
            _log.debug("Hardware detection failed: %s", exc)
            console.print(
                f"  [{design.WARN}]Hardware scan unavailable ({exc}).[/{design.WARN}]"
                f" [{design.MUTED}]Continuing with defaults.[/{design.MUTED}]"
            )
            return

    table = Table(border_style=design.FAINT, padding=(0, 1), show_header=False)
    table.add_column("Property", style=design.MUTED, width=16)
    table.add_column("Value", style=design.WHITE)

    table.add_row("RAM", f"{profile.total_ram_gb:.0f} GB")
    gpu_str = profile.gpu_name or "not detected"
    table.add_row("GPU", gpu_str)
    if profile.vram_total_gb is not None:
        table.add_row("VRAM", f"{profile.vram_total_gb:.0f} GB")
    table.add_row("Tier", profile.tier.value.upper())
    if profile.recommended_model_size and profile.recommended_model_size != "none":
        table.add_row("Best local model", f"up to {profile.recommended_model_size}")

    console.print()
    console.print(table)

    for warning in profile.warnings:
        console.print(f"  [{design.WARN}]{warning}[/{design.WARN}]")
    for suggestion in profile.suggestions:
        console.print(f"  [{design.MUTED}]{suggestion}[/{design.MUTED}]")


# ── Step S6: health check ───────────────────────────────────────────────────────


def _step_health_check(console: Console) -> str:
    """Run 6 critical doctor checks; return a one-line summary string."""
    from velune.cli.commands.doctor import (
        _check_internet_connectivity,
        _check_ollama_connectivity,
        _check_python_version,
        _check_sqlite,
        _check_velune_dir,
    )

    checks = [
        ("Python version", _check_python_version),
        (".velune directory", _check_velune_dir),
        ("SQLite", _check_sqlite),
        ("Internet connectivity", _check_internet_connectivity),
        ("Ollama (local)", _check_ollama_connectivity),
    ]

    # Conditionally add Groq check only if key is configured.
    try:
        from velune.providers.keystore import has_key

        if has_key("groq"):
            from velune.cli.commands.doctor import _check_groq

            checks.append(("Groq API key", _check_groq))
    except Exception:
        pass

    table = Table(border_style=design.FAINT, padding=(0, 1))
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Check", style=design.MUTED)
    table.add_column("Detail", style=design.MUTED)

    results: list[dict] = []
    console.print()
    with console.status(f"  [{design.MUTED}]Running health checks...[/{design.MUTED}]"):
        for name, fn in checks:
            try:
                result = fn()
            except Exception as exc:
                result = {"name": name, "status": "warn", "message": str(exc)}
            results.append(result)

    _STATUS_ICON = {
        "ok":    (design.ICON_SUCCESS, design.OK),
        "warn":  (design.ICON_WARNING, design.WARN),
        "fail":  (design.ICON_ERROR,   design.DANGER),
        "error": (design.ICON_ERROR,   design.DANGER),
    }

    for (check_name, _fn), res in zip(checks, results):
        status = res.get("status", "warn")
        icon, color = _STATUS_ICON.get(status, (design.ICON_WARNING, design.WARN))
        table.add_row(
            f"[{color}]{icon}[/{color}]",
            check_name,
            res.get("message", ""),
        )

    console.print(table)

    failures = [r for r in results if r.get("status") in ("fail", "error")]
    warnings = [r for r in results if r.get("status") == "warn"]

    if failures:
        fail_names = ", ".join(r.get("name", "?") for r in failures)
        console.print(
            f"\n  [{design.WARN}]{len(failures)} check(s) need attention: {fail_names}[/{design.WARN}]\n"
            f"  [{design.MUTED}]Run [bold]velune doctor check[/bold] for full details.[/{design.MUTED}]"
        )
        return f"{len(failures)} check(s) need attention — run `velune doctor check`."

    if warnings:
        return f"Health check complete — {len(warnings)} advisory warning(s). Run `velune doctor check` for details."

    return "All health checks passed."


# ── Step S1: AI mode selection ─────────────────────────────────────────────────


def _step_ai_mode(console: Console) -> Literal["local", "cloud", "hybrid", "skip"]:
    """Prompt the user for their preferred AI mode.  Loops until valid input."""
    console.print(
        f"[{design.INFO}]How would you like to use Velune?[/{design.INFO}]\n\n"
        f"  [bold]1[/bold]  Local AI"
        f"  [{design.MUTED}]· Ollama or LM Studio on this machine (free, private)[/{design.MUTED}]\n"
        f"  [bold]2[/bold]  Cloud AI"
        f"  [{design.MUTED}]· Groq, Anthropic, OpenAI, Gemini, and more[/{design.MUTED}]\n"
        f"  [bold]3[/bold]  Hybrid  "
        f"  [{design.MUTED}]· Local + Cloud (recommended)[/{design.MUTED}]\n"
        f"  [bold]4[/bold]  Skip    "
        f"  [{design.MUTED}]· Enter the REPL now, configure later[/{design.MUTED}]"
    )
    console.print()

    _map: dict[str, str] = {
        "1": "local",
        "2": "cloud",
        "3": "hybrid",
        "4": "skip",
        "local": "local",
        "cloud": "cloud",
        "hybrid": "hybrid",
        "skip": "skip",
    }

    while True:
        raw = (
            Prompt.ask(
                f"  Choice [{design.MUTED}](1-4)[/{design.MUTED}]",
                default="3",
            )
            .strip()
            .lower()
        )

        result = _map.get(raw)
        if result:
            return result  # type: ignore[return-value]

        console.print(f"  [{design.WARN}]Please enter 1, 2, 3, or 4.[/{design.WARN}]")


# ── Step S2: local provider detection ─────────────────────────────────────────


def _step_local_detection(console: Console) -> list[str]:
    """Probe Ollama and LM Studio.  Offers retry on failure (max 3 attempts)."""
    from velune.providers.keystore import is_ollama_live
    from velune.providers.validation import validate_provider_sync

    console.print()
    configured: list[str] = []
    attempt = 0

    while attempt <= _MAX_LOCAL_RETRIES:
        with console.status(f"  [{design.MUTED}]Scanning for local AI servers...[/{design.MUTED}]"):
            ollama_live = is_ollama_live(timeout=1.0)
            if ollama_live:
                ollama_result = validate_provider_sync("ollama", "")
            else:
                ollama_result = None
                lmstudio_result = validate_provider_sync("lmstudio", "")

        if ollama_live and ollama_result and ollama_result.ok:
            n = len(ollama_result.models)
            console.print(
                f"  [{design.OK}]Ollama detected[/{design.OK}]"
                f" [{design.MUTED}]— {n} model{'s' if n != 1 else ''} available[/{design.MUTED}]"
            )
            _show_models_mini(console, ollama_result.models[:5])
            configured.append("ollama")
            return configured

        if not ollama_live and lmstudio_result and lmstudio_result.ok:
            n = len(lmstudio_result.models)
            console.print(
                f"  [{design.OK}]LM Studio detected[/{design.OK}]"
                f" [{design.MUTED}]— {n} model{'s' if n != 1 else ''} available[/{design.MUTED}]"
            )
            _show_models_mini(console, lmstudio_result.models[:5])
            configured.append("lmstudio")
            return configured

        # Nothing found
        console.print(f"\n  [{design.DANGER}]No local AI server detected[/{design.DANGER}]\n")
        console.print(
            f"  [{design.MUTED}]Ollama is a free local AI server that runs models on your machine.[/{design.MUTED}]\n"
            f"  [{design.MUTED}]  Install: [link=https://ollama.com]https://ollama.com[/link][/{design.MUTED}]\n"
            f"  [{design.MUTED}]  Then run: [bold]ollama serve[/bold][/{design.MUTED}]\n"
        )

        if attempt >= _MAX_LOCAL_RETRIES:
            console.print(
                f"  [{design.WARN}]Skipping local AI detection — server not found after"
                f" {_MAX_LOCAL_RETRIES} retries.[/{design.WARN}]"
            )
            return configured

        choice = (
            Prompt.ask(
                "  [[bold]R[/bold]] Retry  [[bold]S[/bold]] Skip  [[bold]Q[/bold]] Quit setup",
                default="R",
            )
            .strip()
            .upper()[:1]
        )

        if choice == "S":
            return configured
        if choice == "Q":
            raise KeyboardInterrupt

        attempt += 1

    return configured


# ── Step S3: cloud provider setup ─────────────────────────────────────────────


def _step_cloud_setup(console: Console) -> list[str]:
    """Show grouped cloud provider table, collect and validate keys."""
    from velune.providers.keystore import has_key

    console.print()

    table = Table(border_style=design.FAINT, padding=(0, 1), show_header=True)
    table.add_column("#", style=design.MUTED, width=3)
    table.add_column("Provider", style=design.INFO)
    table.add_column("Tier", style=design.MUTED, width=6)
    table.add_column("Status")

    for i, (pid, is_free, label, _key_label, _url) in enumerate(_ONBOARDING_CLOUD, 1):
        tier = (
            f"[{design.OK}]free[/{design.OK}]"
            if is_free
            else f"[{design.MUTED}]paid[/{design.MUTED}]"
        )
        status = (
            f"[{design.OK}]configured[/{design.OK}]"
            if has_key(pid)
            else f"[{design.MUTED}]not set[/{design.MUTED}]"
        )
        table.add_row(str(i), label, tier, status)

    console.print(table)
    console.print(
        f"  [{design.MUTED}]Recommended free start: Groq (1) + Google Gemini (2)[/{design.MUTED}]\n"
        f"  [{design.MUTED}]8 more providers available via [bold]velune setup[/bold][/{design.MUTED}]"
    )
    console.print()

    raw = Prompt.ask(
        f"  Select providers [{design.MUTED}](comma-separated numbers, e.g. 1,3 — or Enter to skip)[/{design.MUTED}]",
        default="",
    ).strip()

    if not raw:
        console.print(f"  [{design.MUTED}]Skipped cloud setup.[/{design.MUTED}]")
        return []

    selected_ids: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(_ONBOARDING_CLOUD):
                pid = _ONBOARDING_CLOUD[idx][0]
                if pid not in seen:
                    selected_ids.append(pid)
                    seen.add(pid)

    if not selected_ids:
        console.print(
            f"  [{design.WARN}]No valid providers selected.[/{design.WARN}]"
            f" [{design.MUTED}]Run [bold]velune setup[/bold] to configure later.[/{design.MUTED}]"
        )
        return []

    configured: list[str] = []
    total = len(selected_ids)

    for step_n, pid in enumerate(selected_ids, 1):
        meta = {e[0]: e for e in _ONBOARDING_CLOUD}[pid]
        _, _free, label, key_label, get_key_url = meta

        ok = _step_key_entry(console, pid, label, key_label, get_key_url, step_n, total)
        if ok:
            configured.append(pid)

    return configured


def _step_key_entry(
    console: Console,
    pid: str,
    label: str,
    key_label: str,
    get_key_url: str,
    step_n: int,
    total_steps: int,
) -> bool:
    """Prompt for and validate a single cloud provider API key.

    Returns True if the key was saved (or already existed and was kept).
    """
    from velune.providers.keystore import (
        PROVIDER_ENV_VARS,
        get_key,
        has_key,
    )
    from velune.providers.validation import validate_provider_sync

    console.print()
    console.print(
        f"  [{design.MUTED}]({step_n}/{total_steps})[/{design.MUTED}]"
        f" [{design.INFO}]{label}[/{design.INFO}]"
    )
    console.print(f"  [{design.MUTED}]Get your key: {get_key_url}[/{design.MUTED}]")

    # Existing key — offer to keep or replace.
    if has_key(pid):
        existing = get_key(pid)
        masked = _mask_key(existing)
        console.print(f"  [{design.OK}]Key already configured ({masked})[/{design.OK}]")
        overwrite = Confirm.ask("  Replace it?", default=False)
        if not overwrite:
            return True  # Keep existing key

    for attempt in range(_MAX_KEY_ATTEMPTS):
        key = Prompt.ask(
            f"  Enter {key_label} [{design.MUTED}](or Enter to skip)[/{design.MUTED}]",
            password=True,
        )

        if not key.strip():
            console.print(f"  [{design.MUTED}]Skipped.[/{design.MUTED}]")
            return False

        key = key.strip()

        with console.status(f"  [{design.MUTED}]Validating...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, key)

        if result.ok:
            _safe_save_key(console, pid, key, PROVIDER_ENV_VARS)
            console.print(f"  [{design.OK}]{result.human_message()}[/{design.OK}]")
            _show_models_mini(console, result.models[:5])
            return True

        # Failure — show a specific, actionable message.
        console.print(f"\n  [{design.DANGER}]{result.human_message()}[/{design.DANGER}]")

        if result.status.value == "network_error":
            console.print(
                f"  [{design.MUTED}]Could not reach {pid} — your network may be offline.[/{design.MUTED}]"
            )
            choice = (
                Prompt.ask(
                    "  [[bold]T[/bold]] Try again  [[bold]W[/bold]] Save anyway  [[bold]S[/bold]] Skip",
                    default="W",
                )
                .strip()
                .upper()[:1]
            )

            if choice == "W":
                _safe_save_key(console, pid, key, PROVIDER_ENV_VARS)
                console.print(
                    f"  [{design.WARN}]Key saved without validation — verify network later.[/{design.WARN}]"
                )
                return True
            if choice == "S":
                return False
            # T → fall through to retry

        else:
            # Hard failure (invalid/expired/revoked key)
            console.print(f"  [{design.MUTED}]Get a new key: {get_key_url}[/{design.MUTED}]")

            if attempt < _MAX_KEY_ATTEMPTS - 1:
                choice = (
                    Prompt.ask(
                        "  [[bold]T[/bold]] Try again  [[bold]S[/bold]] Skip",
                        default="T",
                    )
                    .strip()
                    .upper()[:1]
                )
                if choice == "S":
                    return False
                # T → next attempt
            else:
                console.print(
                    f"  [{design.WARN}]Too many failed attempts. Skipping {pid}.[/{design.WARN}]"
                )
                return False

    return False


# ── Step S5: model discovery ───────────────────────────────────────────────────


def _step_model_discovery(console: Console) -> list[ModelDescriptor]:
    """Run ModelDiscoveryScanner.scan_all() in a fresh event loop with a spinner."""
    from velune.providers.discovery.scanner import ModelDiscoveryScanner

    console.print()
    scanner = ModelDiscoveryScanner()
    models: list = []

    with console.status(f"  [{design.MUTED}]Discovering available models...[/{design.MUTED}]"):
        try:
            # Onboarding runs synchronously before the main event loop starts;
            # a short-lived loop keeps the security gate count at exactly one.
            _loop = asyncio.new_event_loop()
            try:
                models = _loop.run_until_complete(scanner.scan_all())
            finally:
                _loop.close()
        except Exception:
            models = []

    if not models:
        console.print(
            f"  [{design.WARN}]No models discovered.[/{design.WARN}]"
            f" [{design.MUTED}]Check that your providers are reachable.[/{design.MUTED}]"
        )
        return []

    # Group and display counts by provider.
    provider_counts: dict[str, int] = {}
    for m in models:
        provider_counts[m.provider_id] = provider_counts.get(m.provider_id, 0) + 1

    for pid, count in sorted(provider_counts.items()):
        console.print(
            f"  [{design.INFO}]{pid}[/{design.INFO}]"
            f" [{design.MUTED}]{count} model{'s' if count != 1 else ''}[/{design.MUTED}]"
        )

    total = len(models)
    console.print(
        f"\n  [{design.MUTED}]{total} model{'s' if total != 1 else ''} available total.[/{design.MUTED}]"
    )
    return models


# ── Step S6: model recommendation & selection ──────────────────────────────────


def _step_model_recommendation(
    console: Console,
    models: list[ModelDescriptor],
    preferred_local: bool,
) -> bool:
    """Pick and confirm the best model; fall back to manual table selection."""
    from velune.cli.model_prefs import save_active_model

    best = _score_models(models, preferred_local)
    if best is None:
        return _step_manual_model_select(console, models)

    why = _model_why(best, preferred_local)
    content = (
        f"  [{design.MUTED}]Provider [/{design.MUTED}] {best.provider_id}\n"
        f"  [{design.MUTED}]Model    [/{design.MUTED}] [bold]{best.model_id}[/bold]\n"
        f"  [{design.MUTED}]Why      [/{design.MUTED}] [{design.INFO}]{why}[/{design.INFO}]"
    )
    console.print()
    console.print(
        Panel(
            content,
            title=f"[bold {design.ACCENT}]Recommended model[/bold {design.ACCENT}]",
            border_style=design.ACCENT,
            padding=(0, 2),
        )
    )

    accept = Confirm.ask("\n  Use this model?", default=True)
    if accept:
        save_active_model(best.provider_id, best.model_id)
        console.print(f"  [{design.OK}]{best.model_id} set as default model.[/{design.OK}]")
        return True

    return _step_manual_model_select(console, models)


def _step_manual_model_select(
    console: Console,
    models: list[ModelDescriptor],
) -> bool:
    """Numbered table for manual model selection."""
    from velune.cli.model_prefs import save_active_model

    if not models:
        console.print(f"  [{design.WARN}]No models available to select.[/{design.WARN}]")
        return False

    display_models = models[:20]

    table = Table(border_style=design.FAINT, padding=(0, 1))
    table.add_column("#", style=design.MUTED, width=3)
    table.add_column("Provider", style=design.INFO)
    table.add_column("Model", style=design.WHITE)
    table.add_column("Speed", style=design.MUTED, width=7)
    table.add_column("Context", style=design.MUTED, width=8)

    for i, m in enumerate(display_models, 1):
        ctx = f"{m.context_length // 1000}k" if m.context_length >= 1000 else str(m.context_length)
        table.add_row(str(i), m.provider_id, m.model_id, m.speed_tier, ctx)

    console.print()
    console.print(table)

    if len(models) > 20:
        console.print(
            f"  [{design.MUTED}](showing 20 of {len(models)} —"
            f" run [bold]/model list[/bold] for all)[/{design.MUTED}]"
        )

    raw = Prompt.ask(
        f"  Select model [{design.MUTED}](number, or Enter to skip)[/{design.MUTED}]",
        default="",
    ).strip()

    if not raw:
        console.print(
            f"  [{design.MUTED}]No model selected —"
            f" type [bold]/model connect[/bold] in the REPL to choose one.[/{design.MUTED}]"
        )
        return False

    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(display_models):
            chosen = display_models[idx]
            save_active_model(chosen.provider_id, chosen.model_id)
            console.print(f"  [{design.OK}]{chosen.model_id} set as default model.[/{design.OK}]")
            return True

    console.print(
        f"  [{design.WARN}]Invalid selection — run [bold]/model connect[/bold]"
        f" to choose later.[/{design.WARN}]"
    )
    return False


# ── Step S7: workspace detection ───────────────────────────────────────────────

_REPO_MARKERS: tuple[tuple[str, str], ...] = (
    (".git", "Git"),
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
    ("package.json", "Node.js"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java"),
    ("build.gradle", "Kotlin/Java"),
)


def _step_workspace_detection(console: Console, workspace: Path) -> None:
    """Detect a repository in *workspace* and offer to register + index it."""
    from velune.cli.workspaces import WorkspaceRegistry

    repo_name: str | None = None
    project_type: str | None = None

    try:
        for marker, ptype in _REPO_MARKERS:
            if (workspace / marker).exists():
                repo_name = workspace.name or str(workspace)
                project_type = ptype
                break
    except Exception:
        return

    if not repo_name:
        return

    console.print()
    type_str = f" ({project_type})" if project_type else ""
    console.print(
        f"  [{design.INFO}]Repository detected:[/{design.INFO}] [bold]{repo_name}[/bold]{type_str}"
    )

    open_it = Confirm.ask(
        f"  [{design.MUTED}]Open this workspace?"
        f" (lets Velune understand your codebase for better suggestions)[/{design.MUTED}]",
        default=True,
    )

    if not open_it:
        return

    try:
        reg = WorkspaceRegistry()
        reg.register(workspace)
        console.print(f"  [{design.OK}]Workspace registered.[/{design.OK}]")
    except Exception as exc:
        console.print(f"  [{design.WARN}]Could not register workspace: {exc}[/{design.WARN}]")
        return

    index_it = Confirm.ask(
        f"  [{design.MUTED}]Index this project for AI context?"
        f" (runs in background, ~30 seconds)[/{design.MUTED}]",
        default=False,
    )
    if index_it:
        console.print(
            f"  [{design.MUTED}]→ Indexing will start in the background once the REPL opens.[/{design.MUTED}]\n"
            f"  [{design.MUTED}]  Or type [bold]/index[/bold] in the REPL at any time.[/{design.MUTED}]"
        )
    else:
        console.print(
            f"  [{design.MUTED}]  Type [bold]/index[/bold] in the REPL to build AI context later.[/{design.MUTED}]"
        )


# ── Step S8: ready summary ─────────────────────────────────────────────────────


def _step_ready_summary(
    console: Console,
    configured: list[str],
    workspace: Path | None,
    model_pref: object | None,
) -> None:
    """Display the 'Velune is ready' panel with contextual first-prompt examples."""
    lines: list[str] = []

    if model_pref:
        provider = getattr(model_pref, "provider_id", "")
        model_id = getattr(model_pref, "model_id", "")
        lines.append(
            f"  [{design.MUTED}]Model     [/{design.MUTED}] {provider} / [bold]{model_id}[/bold]"
        )
    else:
        lines.append(
            f"  [{design.MUTED}]Model     [/{design.MUTED}]"
            f" [dim]not set — type /model connect[/dim]"
        )

    if configured:
        providers_str = "  ·  ".join(p.title() for p in configured[:5])
        if len(configured) > 5:
            providers_str += f"  [dim]+{len(configured) - 5} more[/dim]"
        lines.append(f"  [{design.MUTED}]Providers [/{design.MUTED}] {providers_str}")
    else:
        lines.append(
            f"  [{design.MUTED}]Providers [/{design.MUTED}]"
            f" [dim]none configured — type /setup[/dim]"
        )

    if workspace:
        ws_name = workspace.name or str(workspace)
        lines.append(f"  [{design.MUTED}]Workspace [/{design.MUTED}] {ws_name}")

    lines.append(f"  [{design.MUTED}]Memory    [/{design.MUTED}] ready")
    lines.append(
        f"  [{design.MUTED}]Index     [/{design.MUTED}]"
        f" [dim]not yet — type /index to build AI context[/dim]"
    )

    # Contextual first-prompt examples based on whether a repo is present.
    has_repo = workspace and any(
        (workspace / m).exists()
        for m in (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")
    )
    if has_repo:
        examples = [
            '"Explain this codebase"',
            '"Find potential bugs in the main module"',
            '"How does the authentication flow work?"',
        ]
    else:
        examples = [
            '"Write a Python function to parse JSON"',
            '"Explain async/await in simple terms"',
            '"Help me debug this error: ..."',
        ]

    suggestions = "\n".join(f"  [{design.MUTED}]{ex}[/{design.MUTED}]" for ex in examples)

    content = (
        f"[bold {design.ACCENT}]Velune is ready.[/bold {design.ACCENT}]\n\n"
        + "\n".join(lines)
        + f"\n\n[{design.MUTED}]Try asking:[/{design.MUTED}]\n"
        + suggestions
    )

    console.print()
    console.print(Panel(content, border_style=design.GREEN, padding=(0, 2)))


# ── Degraded mode (user skipped or 0 providers configured) ────────────────────


def _step_degraded_mode(console: Console) -> None:
    console.print()
    console.print(
        Panel(
            f"[{design.WARN}]No AI providers configured[/{design.WARN}]\n\n"
            f"[{design.MUTED}]Velune is starting without a provider. You can:\n"
            f"  Type [bold]/setup[/bold] in the REPL to configure providers\n"
            f"  Run [bold]velune setup[/bold] in a new terminal\n"
            f"  Set environment variables (e.g. [bold]GROQ_API_KEY[/bold])"
            f" and restart Velune[/{design.MUTED}]",
            border_style=design.WARN,
            padding=(0, 2),
        )
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _safe_save_key(
    console: Console,
    pid: str,
    key: str,
    env_vars: dict[str, str],
) -> None:
    """Save *key* for *pid* to the OS keyring; print a fallback hint on failure."""
    from velune.providers.keystore import save_key

    try:
        save_key(pid, key)
    except Exception as exc:
        env_var = env_vars.get(pid, f"{pid.upper()}_API_KEY")
        console.print(
            f"  [{design.WARN}]OS keyring unavailable: {exc}[/{design.WARN}]\n"
            f"  [{design.MUTED}]Set [bold]{env_var}=<your-key>[/bold] in your environment"
            f" as a fallback.[/{design.MUTED}]"
        )


def _show_models_mini(console: Console, model_ids: list[str]) -> None:
    """Print a compact list of model IDs below a success message."""
    if not model_ids:
        return
    preview = ", ".join(model_ids[:5])
    console.print(f"  [{design.MUTED}]  {preview}[/{design.MUTED}]")


def _mask_key(key: str | None) -> str:
    """Return a masked display version of *key*."""
    if not key:
        return "***"
    if len(key) <= 12:
        return "*" * len(key)
    return key[:6] + "..." + key[-4:]


def _score_models(
    models: list[ModelDescriptor],
    preferred_local: bool,
) -> ModelDescriptor | None:
    """Pick the best default model using a lightweight scoring heuristic.

    Does not require Tier-1 subsystems — works purely from the descriptor
    fields returned by ``ModelDiscoveryScanner.scan_all()``.
    """
    if not models:
        return None

    def _cap(m: ModelDescriptor) -> int:
        caps = m.capabilities
        if caps is None:
            return 0
        if hasattr(caps, "coding"):
            return int(caps.coding or 0) + int(getattr(caps, "reasoning", 0) or 0)
        if isinstance(caps, dict):
            return int(caps.get("coding", 0)) + int(caps.get("reasoning", 0))
        return 0

    def _score(m: ModelDescriptor) -> tuple[int, ...]:
        cap = _cap(m)
        local_bonus = 8 if (preferred_local and m.is_local) else (3 if m.is_local else 0)
        speed_bonus = {"fast": 6, "medium": 3, "slow": 0}.get(m.speed_tier, 0)
        ctx_score = min(m.context_length // 10_000, 12)
        # Prefer non-zero cost models that are cheap over "unknown" local models
        # (free cloud tiers are fine; local beats paid when local preference is set)
        return (cap + local_bonus + speed_bonus + ctx_score,)

    return max(models, key=_score)


def _model_why(model: ModelDescriptor, preferred_local: bool) -> str:
    """Generate a short human-readable reason string for the recommended model."""
    parts: list[str] = []

    if model.is_local:
        parts.append("local · private · free")
    elif model.cost_per_1k_tokens is not None:
        if model.cost_per_1k_tokens < 0.001:
            parts.append("very affordable")
        elif model.cost_per_1k_tokens < 0.01:
            parts.append("affordable")

    if model.speed_tier == "fast":
        parts.append("fast inference")
    elif model.speed_tier == "slow":
        parts.append("highest quality")

    ctx_k = model.context_length // 1_000
    if ctx_k >= 32:
        parts.append(f"{ctx_k}k context")

    caps = model.capabilities
    coding_score = 0
    if hasattr(caps, "coding"):
        coding_score = int(caps.coding or 0)
    elif isinstance(caps, dict):
        coding_score = int(caps.get("coding", 0))

    if coding_score >= 75:
        parts.append("strong coding")

    return "  ·  ".join(parts) if parts else "best available match"
