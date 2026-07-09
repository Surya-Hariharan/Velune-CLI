"""The 8 onboarding stages, rebuilt on the interactive widget primitives.

Stage *content* is unchanged from the former ``onboarding.py`` (same hardware
detection, same health checks, same model-scoring heuristic, same workspace
markers — all in ``logic.py``); only the *interaction* is new: arrow-key
menus and checkbox checklists instead of ``Prompt.ask``-with-numbers, one
persistent full-screen wizard instead of an ever-growing scroll, an always
-visible sidebar, and immediate key validation.

Esc ("back") returns to the previous top-level stage (not fine-grained
sub-step undo — going back re-runs that whole stage from its own start,
which is simple to reason about and covers the common case). Ctrl-C aborts
the entire wizard via ``WizardCancelled``, handled by ``WizardController.run``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune.cli.interactive.chrome import StageInfo, WizardCancelled, WizardController
from velune.cli.interactive.result import BACK
from velune.cli.interactive.widgets import ConfirmWidget, Option, SelectWidget, TextInputWidget
from velune.cli.onboarding import logic
from velune.providers import catalog

if TYPE_CHECKING:
    from velune.core.types.model import ModelDescriptor

STAGES: list[StageInfo] = [
    StageInfo("welcome", "Welcome"),
    StageInfo("detect_environment", "Environment"),
    StageInfo("configure_providers", "Providers"),
    StageInfo("discover_models", "Discover Models"),
    StageInfo("select_default_model", "Select Model"),
    StageInfo("health_check", "Health Check"),
    StageInfo("workspace_setup", "Workspace"),
    StageInfo("ready", "Ready"),
]

BRAND = "Velune Setup"


async def run(runtime: object, start_stage: int = 0) -> None:
    """Build the wizard chrome and drive all 8 stages."""
    controller = WizardController(BRAND, STAGES)
    for name in logic._STAGE_NAMES[:start_stage]:
        controller.mark_complete(name)

    try:
        workspace_val = runtime.container.get("runtime.workspace")  # type: ignore[attr-defined]
        workspace = Path(workspace_val) if workspace_val else Path.cwd()
    except Exception:
        workspace = Path.cwd()

    await controller.run(lambda: _drive(controller, start_stage, workspace))


async def _continue_screen(
    controller: WizardController,
    idx: int,
    title: str,
    body: str,
    *,
    subtitle: str = "",
) -> Any:
    widget = SelectWidget(
        title=title,
        subtitle=(subtitle + "\n\n" + body) if subtitle else body,
        options=[Option("continue", "Continue")],
    )
    return await controller.run_widget(widget, stage_index=idx)


# ── Driver: walks the 8 stages, honoring Esc-to-go-back ─────────────────────


async def _drive(controller: WizardController, start_stage: int, workspace: Path) -> None:
    mode = "hybrid"
    configured: list[str] = []
    preferred_local = False
    models: list[ModelDescriptor] = []

    idx = start_stage
    while idx < len(STAGES):
        try:
            if idx == 0:
                result = await _stage_welcome(controller)
                if result == "skip":
                    await _stage_degraded(controller)
                    return
                if result is BACK:
                    continue  # nothing before Welcome; re-show it
                mode = result
                controller.mark_complete("welcome")
                logic.save_stage_progress("welcome")

            elif idx == 1:
                result = await _stage_environment(controller)
                if result is BACK:
                    idx -= 1
                    continue
                controller.mark_complete("detect_environment")
                logic.save_stage_progress("detect_environment")

            elif idx == 2:
                result = await _stage_providers(controller, mode)
                if result is BACK:
                    idx -= 1
                    continue
                configured = result
                if not configured:
                    await _stage_degraded(controller)
                    return
                preferred_local = mode in ("local", "hybrid")
                controller.mark_complete("configure_providers")
                logic.save_stage_progress("configure_providers")

            elif idx == 3:
                result = await _stage_discover_models(controller)
                if result is BACK:
                    idx -= 1
                    continue
                models = result
                controller.mark_complete("discover_models")
                logic.save_stage_progress("discover_models")

            elif idx == 4:
                result = await _stage_select_model(controller, models, preferred_local)
                if result is BACK:
                    idx -= 1
                    continue
                controller.mark_complete("select_default_model")
                logic.save_stage_progress("select_default_model")

            elif idx == 5:
                result = await _stage_health_check(controller)
                if result is BACK:
                    idx -= 1
                    continue
                controller.mark_complete("health_check")
                logic.save_stage_progress("health_check")

            elif idx == 6:
                result = await _stage_workspace(controller, workspace)
                if result is BACK:
                    idx -= 1
                    continue
                controller.mark_complete("workspace_setup")
                logic.save_stage_progress("workspace_setup")

            elif idx == 7:
                await _stage_ready(controller, workspace)
                controller.mark_complete("ready")
                logic.save_stage_progress("ready")
                logic.mark_onboarding_complete()

            if idx != 7:
                await _stage_transition(controller, idx)
            idx += 1
        except WizardCancelled:
            raise


async def _stage_transition(controller: WizardController, idx: int) -> None:
    """Brief 'Loading next step...' frame between stages (spec item 5)."""
    title = STAGES[idx].title
    await controller.show_transient(
        [
            [
                ("fg:#ff7fb6", f"  ✓ {title} complete\n\n"),
                ("fg:#d9a8c0", "  Loading next step..."),
            ]
        ],
        delay=0.35,
    )


# ── Stage 1: Welcome ─────────────────────────────────────────────────────────


async def _stage_welcome(controller: WizardController) -> Any:
    widget = SelectWidget(
        title="How would you like to use Velune?",
        subtitle=(
            "Connects to local AI (Ollama, LM Studio) and cloud providers"
            " (Groq, Anthropic, OpenAI, and more).\n"
            "Everything is stored locally — keys in your OS keychain, data in ~/.velune."
        ),
        options=[
            Option("hybrid", "Hybrid (Recommended)", meta="Local + Cloud"),
            Option("local", "Local AI", meta="Ollama or LM Studio — free, private"),
            Option("cloud", "Cloud AI", meta="Groq, Anthropic, OpenAI, Gemini, and more"),
            Option("skip", "Skip Setup", meta="Enter the REPL now, configure later"),
        ],
        initial_index=0,
    )
    return await controller.run_widget(widget, stage_index=0)


# ── Stage 2: Detect Environment ──────────────────────────────────────────────


async def _stage_environment(controller: WizardController) -> Any:
    from velune.hardware.detector import HardwareDetector

    check_names = ["CPU", "RAM", "GPU", "Ollama", "Environment"]
    frames = []
    for i in range(len(check_names)):
        lines = [("bold fg:#ff5fa2", "  Checking hardware...\n\n")]
        for j, name in enumerate(check_names):
            if j < i:
                lines.append(("fg:#ff7fb6", f"  ✓ {name}\n"))
            elif j == i:
                lines.append(("fg:#d9a8c0", f"  … {name}\n"))
            else:
                lines.append(("fg:#9a6f82", f"    {name}\n"))
        frames.append(lines)
    final_lines = [("bold fg:#ff5fa2", "  Checking hardware...\n\n")]
    for name in check_names:
        final_lines.append(("fg:#ff7fb6", f"  ✓ {name}\n"))
    frames.append(final_lines)

    detect_task = asyncio.ensure_future(asyncio.to_thread(HardwareDetector().detect))
    await controller.show_transient(frames, delay=0.12, final_delay=0.2)
    try:
        profile = await detect_task
    except Exception:
        return await _continue_screen(
            controller,
            1,
            "Your System",
            "Hardware scan unavailable. Continuing with defaults.",
        )

    card_lines = [
        f"  RAM               {profile.total_ram_gb:.0f} GB",
        f"  GPU               {profile.gpu_name or 'Integrated / not detected'}",
    ]
    if profile.vram_total_gb is not None:
        card_lines.append(f"  VRAM              {profile.vram_total_gb:.0f} GB")
    card_lines.append(f"  Tier              {profile.tier.value.upper()}")
    if profile.recommended_model_size and profile.recommended_model_size != "none":
        card_lines.append(f"  Recommended Local {profile.recommended_model_size}")
    card_lines.append("  Recommended Cloud Groq")
    for warning in profile.warnings:
        card_lines.append(f"\n  ⚠ {warning}")
    for suggestion in profile.suggestions:
        card_lines.append(f"  · {suggestion}")

    return await _continue_screen(controller, 1, "Your System", "\n".join(card_lines))


# ── Stage 3: Configure Providers ─────────────────────────────────────────────


async def _stage_providers(controller: WizardController, mode: str) -> Any:
    """One alphabetical checklist mixing local and cloud providers — selecting a
    local row (Ollama, LM Studio) triggers a connectivity check, selecting a
    cloud row triggers the key-entry sub-flow. Filtered by the mode chosen in
    Welcome so "local"/"cloud" only show the relevant rows.
    """
    from velune.providers.keystore import has_key, is_ollama_live

    if mode == "local":
        providers = catalog.list_local_providers_alphabetical()
    elif mode == "cloud":
        providers = catalog.list_cloud_providers_alphabetical()
    else:
        providers = catalog.list_providers_alphabetical()

    ollama_live = await asyncio.to_thread(is_ollama_live, 1.0)

    def _is_configured(pid: str) -> bool:
        return ollama_live if pid == "ollama" else has_key(pid)

    checked = {p.id for p in providers if _is_configured(p.id)}

    while True:
        options = [
            Option(
                p.id,
                p.display_name,
                meta="free tier" if p.free_tier else ("local" if not p.requires_key else "paid"),
                badge="✓ already configured" if _is_configured(p.id) else None,
            )
            for p in providers
        ]
        recommended = ", ".join(
            catalog.get(pid).display_name
            for pid in catalog.RECOMMENDED_FREE_START
            if catalog.get(pid)
        )
        subtitle = f"Recommended free start: {recommended}" if recommended else ""

        result = await controller.run_widget(
            SelectWidget(
                title="Select Providers",
                subtitle=subtitle,
                options=options,
                multiple=True,
                initial_checked=frozenset(checked),
            ),
            stage_index=2,
        )
        if result is BACK:
            return BACK

        selected: list[str] = result
        configured_now = {p.id for p in providers if _is_configured(p.id)}
        need_setup = [pid for pid in selected if pid not in configured_now]
        already = [pid for pid in selected if pid in configured_now]

        went_back = False
        for pid in need_setup:
            meta = catalog.get(pid)
            if meta is not None and not meta.requires_key:
                outcome = await _detect_one_local_provider(controller, pid)
            else:
                outcome = await _configure_one_provider_key(controller, pid)
            if outcome is BACK:
                checked = set(selected)
                went_back = True
                break
        if went_back:
            continue

        replace_outcome = await _offer_replace_existing(controller, already)
        if replace_outcome is BACK:
            checked = set(selected)
            continue

        return already + need_setup


async def _detect_one_local_provider(controller: WizardController, pid: str) -> Any:
    from velune.providers.validation import validate_provider

    meta = catalog.get(pid)
    name = meta.display_name if meta else pid

    for _attempt in range(3):
        await controller.show_transient([[("fg:#d9a8c0", f"  Checking {name}...")]], delay=0.3)
        result = await validate_provider(pid, "")
        if result.ok:
            n = len(result.models)
            await controller.show_transient(
                [
                    [
                        (
                            "fg:#ff7fb6",
                            f"  ✓ {name} detected — {n} model{'s' if n != 1 else ''} available.",
                        )
                    ]
                ],
                delay=0.4,
            )
            return pid

        choice = await controller.run_widget(
            SelectWidget(
                title=f"{name} not detected",
                subtitle=(f"Get it running, then retry: {meta.get_key_url}" if meta else ""),
                options=[
                    Option("retry", "Retry"),
                    Option("skip", "Skip"),
                    Option("back", "Back"),
                ],
            ),
            stage_index=2,
        )
        if choice is BACK or choice == "back":
            return BACK
        if choice == "skip":
            return None

    return None


async def _offer_replace_existing(
    controller: WizardController, already_configured: list[str]
) -> Any:
    """Spec item 9: never re-ask for a configured provider's key by default —
    only offer to replace it if the user explicitly asks."""
    remaining = list(already_configured)
    while True:
        options = [
            Option(pid, f"Replace {catalog.get(pid).display_name} key")
            for pid in remaining
            if catalog.get(pid) and catalog.get(pid).requires_key
        ]
        if not options:
            return None
        options.append(Option("none", "No changes"))

        choice = await controller.run_widget(
            SelectWidget(
                title="Replace an existing key?",
                subtitle="Already configured — leave as-is, or replace a key.",
                options=options,
            ),
            stage_index=2,
        )
        if choice is BACK:
            return BACK
        if choice == "none":
            return None

        outcome = await _configure_one_provider_key(controller, choice)
        if outcome is BACK:
            return BACK
        remaining = [pid for pid in remaining if pid != choice]
        if not remaining:
            return None


async def _configure_one_provider_key(controller: WizardController, pid: str) -> Any:
    from velune.providers.keystore import save_key
    from velune.providers.validation import validate_provider

    meta = catalog.get(pid)
    if meta is None:
        return None

    for _attempt in range(3):
        key_result = await controller.run_widget(
            TextInputWidget(
                title=f"{meta.display_name} — {meta.key_label}",
                hint=f"Get your key: {meta.get_key_url}",
                password=True,
                optional=True,
            ),
            stage_index=2,
        )
        if key_result is BACK:
            return BACK
        key = (key_result or "").strip()
        if not key:
            return None  # skipped

        await controller.show_transient([[("fg:#d9a8c0", "  Validating...")]], delay=0.2)
        result = await validate_provider(pid, key)

        if result.ok:
            try:
                save_key(pid, key)
            except Exception:
                pass
            await controller.show_transient(
                [[("fg:#ff7fb6", f"  ✓ Connected — {result.human_message()}")]],
                delay=0.4,
            )
            return pid

        choice = await controller.run_widget(
            SelectWidget(
                title="❌ Invalid API Key",
                subtitle=result.human_message(),
                options=[
                    Option("retry", "Retry"),
                    Option("skip", "Skip"),
                    Option("back", "Back"),
                ],
            ),
            stage_index=2,
        )
        if choice is BACK or choice == "back":
            return BACK
        if choice == "skip":
            return None
        # retry: loop again

    return None


# ── Stage 4: Discover Models ─────────────────────────────────────────────────


async def _stage_discover_models(controller: WizardController) -> Any:
    from velune.providers.discovery.scanner import ModelDiscoveryScanner

    await controller.show_transient(
        [[("fg:#d9a8c0", "  Discovering available models...")]], delay=0.3
    )
    try:
        models = await ModelDiscoveryScanner().scan_all()
    except Exception:
        models = []

    if not models:
        result = await _continue_screen(
            controller, 3, "Discover Models", "No models discovered — check provider connectivity."
        )
        return [] if result is BACK else []

    provider_counts: dict[str, int] = {}
    for m in models:
        provider_counts[m.provider_id] = provider_counts.get(m.provider_id, 0) + 1
    lines = [
        f"  {pid}: {count} model{'s' if count != 1 else ''}"
        for pid, count in sorted(provider_counts.items())
    ]
    lines.append(f"\n  {len(models)} model(s) available total.")

    result = await _continue_screen(controller, 3, "Discover Models", "\n".join(lines))
    if result is BACK:
        return BACK
    return models


# ── Stage 5: Select Default Model ────────────────────────────────────────────


async def _stage_select_model(
    controller: WizardController,
    models: list[ModelDescriptor],
    preferred_local: bool,
) -> Any:
    from velune.cli.model_prefs import save_active_model

    if not models:
        result = await _continue_screen(
            controller,
            4,
            "Select Default Model",
            "No models available — use /model connect in the REPL later.",
        )
        return BACK if result is BACK else True

    best = logic.score_models(models, preferred_local)
    if best is None:
        return await _manual_model_select(controller, models)

    why = logic.model_why(best, preferred_local)
    choice = await controller.run_widget(
        SelectWidget(
            title="Recommended model",
            subtitle=f"Provider  {best.provider_id}\nModel     {best.model_id}\nWhy       {why}",
            options=[
                Option("use", "Use this model"),
                Option("manual", "Choose manually"),
            ],
        ),
        stage_index=4,
    )
    if choice is BACK:
        return BACK
    if choice == "use":
        save_active_model(best.provider_id, best.model_id)
        return True
    return await _manual_model_select(controller, models)


async def _manual_model_select(controller: WizardController, models: list[ModelDescriptor]) -> Any:
    from velune.cli.model_prefs import save_active_model

    display_models = models[:30]
    options = [
        Option(
            f"{m.provider_id}::{m.model_id}",
            m.model_id,
            meta=f"{m.provider_id} · {m.speed_tier} · {m.context_length // 1000}k ctx",
        )
        for m in display_models
    ]
    choice = await controller.run_widget(
        SelectWidget(
            title="Choose a model",
            subtitle="↑↓ navigate  ·  type to filter" if len(display_models) > 8 else "",
            options=options,
            filterable=True,
        ),
        stage_index=4,
    )
    if choice is BACK:
        return BACK
    provider_id, model_id = choice.split("::", 1)
    save_active_model(provider_id, model_id)
    return True


# ── Stage 6: Health Check ────────────────────────────────────────────────────


async def _stage_health_check(controller: WizardController) -> Any:
    checks = logic.build_health_checks()
    names = [name for name, _fn in checks]

    frames = []
    for i in range(len(names)):
        lines = [("bold fg:#ff5fa2", "  Running health checks...\n\n")]
        for j, name in enumerate(names):
            if j < i:
                lines.append(("fg:#ff7fb6", f"  ✓ {name}\n"))
            elif j == i:
                lines.append(("fg:#d9a8c0", f"  … {name}\n"))
            else:
                lines.append(("fg:#9a6f82", f"    {name}\n"))
        frames.append(lines)

    results: list[dict] = []

    async def _run() -> None:
        nonlocal results
        results = await asyncio.to_thread(logic.run_health_checks)

    run_task = asyncio.ensure_future(_run())
    await controller.show_transient(frames, delay=0.15)
    await run_task

    icon = {"ok": "✓", "warn": "⚠", "fail": "✗", "error": "✗"}
    lines = []
    for (name, _fn), res in zip(checks, results, strict=False):
        status = res.get("status", "warn")
        lines.append(f"  {icon.get(status, '·')} {name}  —  {res.get('message', '')}")

    failures = [r for r in results if r.get("status") in ("fail", "error")]
    if failures:
        lines.append(f"\n  {len(failures)} check(s) need attention — run `velune doctor check`.")

    result = await _continue_screen(controller, 5, "Health Check", "\n".join(lines))
    return BACK if result is BACK else True


# ── Stage 7: Workspace Setup ──────────────────────────────────────────────────


async def _stage_workspace(controller: WizardController, workspace: Path) -> Any:
    marker = logic.detect_repo_marker(workspace)
    if marker is None:
        return True

    repo_name, project_type = marker
    open_it = await controller.run_widget(
        ConfirmWidget(
            question=f"Repository detected: {repo_name} ({project_type})",
            hint="Open this workspace? Lets Velune understand your codebase for better suggestions.",
            default=True,
        ),
        stage_index=6,
    )
    if open_it is BACK:
        return BACK
    if not open_it:
        return True

    try:
        from velune.cli.workspaces import WorkspaceRegistry

        WorkspaceRegistry().register(workspace)
    except Exception:
        return True

    await controller.run_widget(
        ConfirmWidget(
            question="Index this project for AI context now?",
            hint="Runs in the background, ~30 seconds. You can also run /index later.",
            default=False,
        ),
        stage_index=6,
    )
    return True


# ── Stage 8: Ready ────────────────────────────────────────────────────────────


async def _stage_ready(controller: WizardController, workspace: Path) -> None:
    from velune.cli.model_prefs import load_active_model
    from velune.providers.keystore import list_configured_providers

    configured = list_configured_providers()
    model_pref = load_active_model()

    lines = ["🎉 Velune is Ready\n"]
    lines.append("Providers")
    if configured:
        for p in configured:
            lines.append(f"  ✓ {p.title()}")
    else:
        lines.append("  none configured — type /setup in the REPL")
    lines.append("")
    lines.append("Model")
    if model_pref:
        lines.append(f"  {model_pref.provider_id} / {model_pref.model_id}")
    else:
        lines.append("  not set — type /model connect")
    lines.append("")
    lines.append("Project")
    lines.append(f"  {workspace.name or workspace}")

    await controller.run_widget(
        SelectWidget(
            title="Setup complete",
            subtitle="\n".join(lines),
            options=[Option("launch", "Press Enter to Launch")],
        ),
        stage_index=7,
    )


# ── Degraded mode ─────────────────────────────────────────────────────────────


async def _stage_degraded(controller: WizardController) -> None:
    await controller.run_widget(
        SelectWidget(
            title="No AI providers configured",
            subtitle=(
                "Velune is starting without a provider. You can:\n"
                "  Type /setup in the REPL to configure providers\n"
                "  Run velune setup in a new terminal\n"
                "  Set environment variables (e.g. GROQ_API_KEY) and restart"
            ),
            options=[Option("continue", "Continue to REPL")],
        ),
        stage_index=controller.current_index,
    )
