"""Model management slash command handlers: /model /models /pull /delete /bench."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from velune.cli import design

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL
    from velune.core.types.model import ModelDescriptor

_log = logging.getLogger("velune.cli.handlers.model")

# Row styles for the model picker. Previously each of these was a hardcoded
# ANSI name (ansicyan/ansiyellow/ansimagenta/ansigray), which is why the picker
# never matched the monochrome theme the rest of the CLI uses — and one of them,
# "ansicenter", was not a real color at all and raised on parse.
_SEL_BG = f"bg:{design.LIGHT_BG} "


def _sel(style: str, selected: bool) -> str:
    """Apply the selected-row background to *style*."""
    return f"{_SEL_BG}{style}" if selected else style


async def cmd_model(repl: VeluneREPL, args: str) -> None:
    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub == "discover":
        return await _model_discover(repl)
    if sub == "connect":
        return await _model_connect(repl, rest)
    if sub == "use":
        return await _model_use(repl, rest)
    if sub == "list":
        return await cmd_models(repl, "")
    if sub == "status":
        return await _model_status(repl)
    if sub == "remove":
        return await _model_remove(repl, rest)
    if sub == "locate":
        return await _model_locate(repl)
    if sub == "locations":
        return await _model_locations(repl, rest)

    model_registry = repl._require("runtime.model_registry", "model registry")
    if model_registry is None:
        return
    provider_registry = repl._require("runtime.provider_registry", "provider registry")
    if provider_registry is None:
        return

    if args.strip():
        model = model_registry.get(args.strip())
        if model:
            # Route through activate_model like /model use and /model connect do.
            # Setting repl.active_model directly skipped persistence, the recents
            # list, and the default-provider write, so `/model <name>` silently
            # behaved differently from every other way of switching models.
            await activate_model(repl, model)
        else:
            from velune.cli.rendering.error_panel import render_error
            from velune.core.errors.catalog import ModelNotFoundError

            repl.console.print(render_error(ModelNotFoundError(f"'{args.strip()}'")))
        return

    models = model_registry.list_all()
    if not models:
        from velune.cli.rendering.error_panel import render_error
        from velune.core.errors.catalog import NoModelsAvailableError

        repl.console.print(render_error(NoModelsAvailableError()))
        return

    selected = await _show_model_picker(repl, models)
    if selected:
        await activate_model(repl, selected)


RECOMMENDED_MODELS = [
    {
        "model_id": "claude-3-5-sonnet-20241022",
        "provider_id": "anthropic",
        "display_name": "Claude 3.5 Sonnet",
        "context_length": 200000,
        "is_local": False,
        "speed_tier": "medium",
        "capabilities": ["coding", "reasoning", "planning", "vision", "tool_use"],
    },
    {
        "model_id": "gpt-4o",
        "provider_id": "openai",
        "display_name": "GPT-4o",
        "context_length": 128000,
        "is_local": False,
        "speed_tier": "fast",
        "capabilities": ["coding", "reasoning", "planning", "vision", "tool_use"],
    },
    {
        "model_id": "gemini-1.5-pro",
        # "google", not "gemini" — the registry, catalog, keystore, and
        # validators all key on "google". Selecting this entry with the wrong id
        # produced a ProviderNotFoundError at inference time.
        "provider_id": "google",
        "display_name": "Gemini 1.5 Pro",
        "context_length": 1048576,
        "is_local": False,
        "speed_tier": "medium",
        "capabilities": ["coding", "reasoning", "planning", "vision", "tool_use"],
    },
    {
        "model_id": "qwen2.5-coder:7b",
        "provider_id": "ollama",
        "display_name": "Qwen 2.5 Coder 7B",
        "context_length": 32000,
        "is_local": True,
        "speed_tier": "fast",
        "capabilities": ["coding", "reasoning", "tool_use"],
    },
    {
        "model_id": "llama3.1:8b",
        "provider_id": "ollama",
        "display_name": "Llama 3.1 8B",
        "context_length": 128000,
        "is_local": True,
        "speed_tier": "fast",
        "capabilities": ["coding", "tool_use"],
    },
    {
        "model_id": "deepseek-r1:7b",
        "provider_id": "ollama",
        "display_name": "DeepSeek R1 7B",
        "context_length": 16384,
        "is_local": True,
        "speed_tier": "fast",
        "capabilities": ["coding", "reasoning"],
    },
]


async def _show_model_picker(
    repl: VeluneREPL, models: list[ModelDescriptor]
) -> ModelDescriptor | None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from velune.cli.autocomplete import fuzzy_score
    from velune.cli.model_prefs import load_favorites, load_recents, toggle_favorite
    from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
    from velune.providers.ollama_manager import OllamaManager

    # Pre-fetch local Ollama models to check downloaded status
    try:
        local_ollama_names = await OllamaManager().list_local_models()
    except Exception:
        local_ollama_names = []

    selected_index = [0]
    filter_text = [""]
    result: list[ModelDescriptor | None] = [None]

    def match_score(query: str, m: ModelDescriptor) -> int:
        if not query:
            return 1
        s1 = fuzzy_score(query, m.model_id)
        s2 = fuzzy_score(query, m.display_name or "")
        s3 = fuzzy_score(query, m.provider_id)
        return max(s1, s2, s3)

    def check_reasoning(m: ModelDescriptor) -> bool:
        if m.capabilities and hasattr(m.capabilities, "reasoning"):
            if getattr(m.capabilities, "reasoning", CapabilityLevel.NONE) >= CapabilityLevel.BASIC:
                return True
        name = m.model_id.lower()
        return any(x in name for x in ("reasoner", "r1", "o1", "o3", "thinking"))

    def check_vision(m: ModelDescriptor) -> bool:
        if m.capabilities and hasattr(m.capabilities, "multimodal"):
            if getattr(m.capabilities, "multimodal", CapabilityLevel.NONE) >= CapabilityLevel.BASIC:
                return True
        name = m.model_id.lower()
        return any(x in name for x in ("vision", "vl", "multimodal", "gpt-4o", "claude", "gemini"))

    def _get_flat_items() -> list[dict]:
        # 1. Load favorites and recents
        favorites = load_favorites()
        recents = load_recents()

        # 2. Add recommended models if they aren't registered yet
        all_models = list(models)
        registered_ids = {m.model_id.lower() for m in all_models}
        for rec in RECOMMENDED_MODELS:
            if rec["model_id"].lower() not in registered_ids:
                caps_list = rec["capabilities"]
                if isinstance(caps_list, list):
                    caps = ModelCapabilityProfile(
                        coding=CapabilityLevel.ADVANCED
                        if "coding" in caps_list
                        else CapabilityLevel.NONE,
                        reasoning=CapabilityLevel.ADVANCED
                        if "reasoning" in caps_list
                        else CapabilityLevel.NONE,
                        planning=CapabilityLevel.ADVANCED
                        if "planning" in caps_list
                        else CapabilityLevel.NONE,
                        multimodal=CapabilityLevel.ADVANCED
                        if "vision" in caps_list
                        else CapabilityLevel.NONE,
                        tool_use=CapabilityLevel.ADVANCED
                        if "tool_use" in caps_list
                        else CapabilityLevel.NONE,
                    )
                else:
                    caps = ModelCapabilityProfile()
                desc = ModelDescriptor(
                    model_id=rec["model_id"],
                    provider_id=rec["provider_id"],
                    display_name=rec["display_name"],
                    context_length=rec["context_length"],
                    capabilities=caps,
                    is_local=rec["is_local"],
                    speed_tier=rec["speed_tier"],
                )
                all_models.append(desc)

        # 3. Filter using fuzzy search
        query = filter_text[0].strip().lower()
        if query:
            scored = []
            for m in all_models:
                score = match_score(query, m)
                if score > 0:
                    scored.append((score, m))
            filtered = [m for _, m in sorted(scored, key=lambda t: -t[0])]
        else:
            filtered = sorted(all_models, key=lambda m: (not m.is_local, m.model_id))

        # Group models into categories
        favs_group = []
        recents_group = []
        installed_group = []
        cloud_group = []
        rec_group = []

        connected_providers = set()
        provider_registry = repl.container.get("runtime.provider_registry")
        for m in all_models:
            if provider_registry.check_provider_available(m.provider_id):
                connected_providers.add(m.provider_id)

        for m in filtered:
            mid = m.model_id
            is_fav = mid in favorites
            is_rec = mid in recents

            # Determine if installed/connected
            is_installed = False
            if m.is_local:
                is_installed = any(
                    m.model_id.lower() in name.lower() or name.lower() in m.model_id.lower()
                    for name in local_ollama_names
                )
            else:
                is_installed = m.provider_id in connected_providers

            if is_fav:
                favs_group.append(m)
            if is_rec:
                recents_group.append(m)
            if is_installed:
                installed_group.append(m)
            if not m.is_local:
                cloud_group.append(m)

            is_recommended = any(
                rec["model_id"].lower() == mid.lower() for rec in RECOMMENDED_MODELS
            )
            if is_recommended:
                rec_group.append(m)

        categories = [
            ("Recent Models", recents_group),
            ("Favorites", favs_group),
            ("Installed Models", installed_group),
            ("Cloud Models", cloud_group),
            ("Recommended", rec_group),
        ]

        flat = []
        for cat_name, models_in_cat in categories:
            # Skip optional categories like "Recent Models" or "Favorites" if empty and no search
            if not models_in_cat and cat_name in ("Recent Models", "Favorites") and not query:
                continue
            flat.append(
                {
                    "type": "header",
                    "category": cat_name,
                    "model": None,
                    "text": FormattedText(
                        [(f"bold fg:{design.ACCENT_SOFT}", f"\n  — {cat_name} —\n")]
                    ),
                }
            )
            if not models_in_cat:
                placeholder_text = (
                    "     (no favorited models - press 'f' to favorite a highlighted model)\n"
                    if cat_name == "Favorites"
                    else "     (no matching models)\n"
                )
                flat.append(
                    {
                        "type": "placeholder",
                        "category": cat_name,
                        "model": None,
                        "text": FormattedText([(f"fg:{design.FAINT}", placeholder_text)]),
                    }
                )
            else:
                for m in models_in_cat:
                    flat.append({"type": "model", "category": cat_name, "model": m, "text": None})
        return flat

    def _render_list() -> FormattedText:
        flat_items = _get_flat_items()
        selectable_indexes = [i for i, item in enumerate(flat_items) if item["type"] == "model"]

        if selectable_indexes:
            selected_index[0] = min(selected_index[0], len(selectable_indexes) - 1)
            selected_index[0] = max(0, selected_index[0])
            highlighted_flat_index = selectable_indexes[selected_index[0]]
        else:
            highlighted_flat_index = -1

        lines: list[tuple[str, str]] = []
        lines.append(
            (
                "bold",
                "  Select a model  (type to filter · ↑↓ navigate · Enter select · [f] favorite · Esc cancel)\n",
            )
        )
        if filter_text[0]:
            lines.append((f"fg:{design.INFO}", f"  Filter: {filter_text[0]}\n"))

        # Table Column Headers — one accent for the primary column, the rest
        # neutral, matching the design system rather than a rainbow of ANSI hues.
        rule = (f"fg:{design.FAINT}", "  " + "—" * 115 + "\n")
        gap = (f"fg:{design.FAINT}", "  ")
        lines.append(rule)
        lines.append((f"bold fg:{design.ACCENT}", f"  {'Model ID / Name':<34}"))
        for header, width in (
            ("Provider", 12),
            ("Context", 8),
            ("Speed", 8),
            ("Reasoning", 14),
            ("Vision", 11),
            ("Status", 12),
        ):
            lines.append(gap)
            lines.append((f"bold fg:{design.SECONDARY}", f"{header:<{width}}"))
        lines.append(gap)
        lines.append((f"bold fg:{design.SECONDARY}", "Capabilities\n"))
        lines.append(rule)

        if not selectable_indexes:
            lines.append((f"fg:{design.WARN}", "\n  No models match your query.\n"))
            return FormattedText(lines)

        favorites = load_favorites()
        provider_registry = repl.container.get("runtime.provider_registry")
        connected_providers = set()
        for m in models:
            if provider_registry.check_provider_available(m.provider_id):
                connected_providers.add(m.provider_id)

        for i, item in enumerate(flat_items):
            if item["type"] != "model":
                lines.extend(item["text"])
                continue

            m = item["model"]
            is_sel = i == highlighted_flat_index
            is_cur = repl.active_model is not None and m.model_id == repl.active_model.model_id

            # Determine if installed/connected
            is_installed = False
            if m.is_local:
                is_installed = any(
                    m.model_id.lower() in name.lower() or name.lower() in m.model_id.lower()
                    for name in local_ollama_names
                )
            else:
                is_installed = m.provider_id in connected_providers

            status = "active" if is_cur else ("installed" if is_installed else "downloadable")

            # Row styles — design tokens throughout, with the selected row
            # carrying a surface background instead of an inverted ANSI block.
            prefix = "❯ " if is_sel else "  "
            row_bg = _SEL_BG if is_sel else ""

            status_color = {
                "active": design.OK,
                "installed": design.SECONDARY,
            }.get(status, design.FAINT)

            model_name_style = _sel(f"bold fg:{design.ACCENT}", is_sel)
            star_style = _sel(f"fg:{design.ACCENT}", is_sel)
            provider_style = _sel(f"fg:{design.MUTED}", is_sel)
            ctx_style = _sel(f"fg:{design.SECONDARY}", is_sel)
            speed_style = _sel(f"fg:{design.INFO}", is_sel)
            status_style = _sel(f"fg:{status_color}", is_sel)
            caps_style = _sel(f"fg:{design.FAINT}", is_sel)

            has_reasoning = check_reasoning(m)
            has_vision = check_vision(m)

            r_style = _sel(f"fg:{design.OK if has_reasoning else design.FAINT}", is_sel)
            v_style = _sel(f"fg:{design.OK if has_vision else design.FAINT}", is_sel)

            # 1. Star + Selection prefix + Name (34 chars total)
            is_fav = m.model_id in favorites
            star_char = "★ " if is_fav else "  "
            name_val = m.display_name or m.model_id
            name_padded = f"{name_val:<30}"[:30]

            lines.append((star_style, f"  {prefix}{star_char}"))
            lines.append((model_name_style, name_padded))

            # 2. Provider (12 chars)
            lines.append((row_bg, "  "))
            lines.append((provider_style, f"{m.provider_id:<12}"[:12]))

            # 3. Context (8 chars)
            if m.context_length >= 1_000_000:
                ctx_str = f"{m.context_length / 1_000_000:g}M"
            else:
                ctx_str = f"{m.context_length // 1000}k"
            lines.append((row_bg, "  "))
            lines.append((ctx_style, f"{ctx_str:<8}"[:8]))

            # 4. Speed (8 chars)
            lines.append((row_bg, "  "))
            lines.append((speed_style, f"{m.speed_tier:<8}"[:8]))

            # 5. Reasoning (14 chars)
            lines.append((row_bg, "  "))
            lines.append((r_style, f"Reasoning: {('Yes' if has_reasoning else 'No'):<3}"))

            # 6. Vision (11 chars)
            lines.append((row_bg, "  "))
            lines.append((v_style, f"Vision: {('Yes' if has_vision else 'No'):<3}"))

            # 7. Status (12 chars)
            lines.append((row_bg, "  "))
            lines.append((status_style, f"{status:<12}"[:12]))

            # 8. Capabilities
            caps_list = []
            if m.capabilities:
                if getattr(m.capabilities, "coding", CapabilityLevel.NONE) >= CapabilityLevel.BASIC:
                    caps_list.append("coding")
                if (
                    getattr(m.capabilities, "planning", CapabilityLevel.NONE)
                    >= CapabilityLevel.BASIC
                ):
                    caps_list.append("planning")
                if (
                    getattr(m.capabilities, "summarization", CapabilityLevel.NONE)
                    >= CapabilityLevel.BASIC
                ):
                    caps_list.append("summary")
                if (
                    getattr(m.capabilities, "tool_use", CapabilityLevel.NONE)
                    >= CapabilityLevel.BASIC
                ):
                    caps_list.append("tool_use")
            caps_str = ", ".join(caps_list) if caps_list else "general"
            lines.append((row_bg, "  "))
            lines.append((caps_style, caps_str))

            lines.append((row_bg, "\n"))

        return FormattedText(lines)

    kb = KeyBindings()

    @kb.add("up")
    def _up(event) -> None:
        flat_items = _get_flat_items()
        selectable_indexes = [i for i, item in enumerate(flat_items) if item["type"] == "model"]
        if selectable_indexes:
            selected_index[0] = (selected_index[0] - 1) % len(selectable_indexes)

    @kb.add("down")
    def _down(event) -> None:
        flat_items = _get_flat_items()
        selectable_indexes = [i for i, item in enumerate(flat_items) if item["type"] == "model"]
        if selectable_indexes:
            selected_index[0] = (selected_index[0] + 1) % len(selectable_indexes)

    # Mouse wheel — same convention as the main REPL transcript (fullscreen.py).
    @kb.add(Keys.ScrollUp, eager=True)
    def _scroll_up(event) -> None:
        _up(event)

    @kb.add(Keys.ScrollDown, eager=True)
    def _scroll_down(event) -> None:
        _down(event)

    @kb.add("enter")
    def _enter(event) -> None:
        flat_items = _get_flat_items()
        selectable_indexes = [i for i, item in enumerate(flat_items) if item["type"] == "model"]
        if selectable_indexes:
            flat_index = selectable_indexes[selected_index[0]]
            result[0] = flat_items[flat_index]["model"]
        event.app.exit()

    @kb.add("f")
    def _favorite(event) -> None:
        flat_items = _get_flat_items()
        selectable_indexes = [i for i, item in enumerate(flat_items) if item["type"] == "model"]
        if selectable_indexes:
            flat_index = selectable_indexes[selected_index[0]]
            model_to_fav = flat_items[flat_index]["model"]
            toggle_favorite(model_to_fav.model_id)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit()

    @kb.add("backspace")
    def _backspace(event) -> None:
        filter_text[0] = filter_text[0][:-1]
        selected_index[0] = 0

    @kb.add("<any>")
    def _type(event) -> None:
        ch = event.data
        if ch and ch.isprintable():
            filter_text[0] += ch
            selected_index[0] = 0

    app = Application(
        layout=Layout(
            Window(
                content=FormattedTextControl(_render_list, focusable=True),
            )
        ),
        key_bindings=kb,
        full_screen=False,
        mouse_support=True,
    )

    await app.run_async()
    return result[0]


async def cmd_models(repl: VeluneREPL, args: str) -> None:
    from velune.core.types.model import CapabilityLevel

    model_registry = repl._require("runtime.model_registry", "model registry")
    if model_registry is None:
        return
    all_models = model_registry.list_all()

    from velune.cli.ui_components import create_table, print_header

    if not all_models:
        from velune.cli.rendering.error_panel import render_error
        from velune.core.errors.catalog import NoModelsAvailableError

        repl.console.print(render_error(NoModelsAvailableError()))
        return

    table = create_table("Model", "Provider", "Type", "Speed", "Context", "Top Skill")

    skill_attrs = ["coding", "reasoning", "planning", "summarization"]
    for m in all_models:
        caps = m.capabilities
        top_skill = "general"
        if caps is not None:
            for attr in skill_attrs:
                level = getattr(caps, attr, CapabilityLevel.NONE)
                if isinstance(level, int) and level >= CapabilityLevel.ADVANCED:
                    top_skill = attr
                    break
        is_active = repl.active_model is not None and m.model_id == repl.active_model.model_id
        name_col = f"[bold]{m.model_id}[/bold] [green](active)[/green]" if is_active else m.model_id
        table.add_row(
            name_col,
            m.provider_id,
            "local" if m.is_local else "cloud",
            m.speed_tier,
            f"{m.context_length // 1000}k",
            top_skill,
        )
    print_header(repl.console, "Available Models")
    repl.console.print(table)
    repl.console.print()
    repl.console.print(
        "[dim]→ /model connect <id> to activate a model  ·  /pull to download more from Ollama[/dim]"
    )


async def activate_model(repl: VeluneREPL, model: ModelDescriptor) -> None:
    """Set *model* as active and persist it as the default for next launch."""
    repl.active_model = model

    # Resize the context meter here rather than waiting for the next render.
    # It was only re-synced during _refresh_status_state, so between the switch
    # and the next frame the tracker still reported the previous model's window
    # — switching 200k Claude to a 16k local model showed headroom that did not
    # exist, while ContextBudget was already using the real value.
    tracker = getattr(repl, "_context_tracker", None)
    if tracker is not None:
        tracker.max_tokens = model.context_length

    from velune.cli.model_prefs import add_recent, save_active_model

    save_active_model(model.provider_id, model.model_id)
    add_recent(model.model_id)
    _persist_default_provider(repl, model.provider_id)
    repl.console.print(
        f"[green]Active model:[/green] [cyan]{model.model_id}[/cyan] "
        f"[dim]{model.provider_id} · ctx {model.context_length:,} · "
        f"{'local' if model.is_local else 'cloud'}[/dim]"
    )


def _persist_default_provider(repl: VeluneREPL, provider_id: str) -> None:
    """Best-effort write of providers.default_provider into velune.toml."""
    from pathlib import Path

    try:
        import toml

        workspace = Path(repl.container.get("runtime.workspace"))
        config_path = repl.container.get("runtime.config_path") or (workspace / "velune.toml")
        config_path = Path(config_path)
        data = toml.load(config_path) if config_path.exists() else {}
        data.setdefault("providers", {})["default_provider"] = provider_id
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as fh:
            toml.dump(data, fh)
    except Exception as exc:
        _log.debug("Could not persist default provider: %s", exc)


def restore_active_model(repl: VeluneREPL) -> None:
    """Restore the persisted default model from the registry, if available."""
    if repl.active_model is not None:
        return
    from velune.cli.model_prefs import load_active_model

    pref = load_active_model()
    if pref is None:
        return
    try:
        registry = repl.container.get("runtime.model_registry")
        model = registry.get(pref.model_id, pref.provider_id) or registry.get(pref.model_id)
    except Exception:
        model = None
    if model is not None:
        repl.active_model = model


async def _model_discover(repl: VeluneREPL) -> None:
    repl.console.print(
        "[dim]Discovering models — Ollama (:11434), LM Studio (:1234), "
        "OpenAI-compatible servers (:8000/:8080/:3000), "
        "and configured cloud providers...[/dim]"
    )
    registry = repl._require("runtime.model_registry", "model registry")
    if registry is None:
        return
    try:
        await registry.refresh()
    except Exception as exc:
        from velune.cli.rendering.error_panel import render_unexpected_error

        repl.console.print(render_unexpected_error(exc))
        return
    models = registry.list_all()
    if not models:
        repl.console.print(
            "[yellow]No models discovered.[/yellow]\n"
            "[dim]→ Start Ollama ([bold]ollama serve[/bold]) or LM Studio, "
            "or configure an API key, then run [bold]/model discover[/bold] again.[/dim]"
        )
        return
    restore_active_model(repl)
    provider_registry = repl.container.get("runtime.provider_registry")
    available = [
        m for m in models if m.is_local or provider_registry.check_provider_available(m.provider_id)
    ]
    pool = available or models
    repl.console.print(f"[dim]Discovered {len(pool)} model(s). Select one (Esc to skip):[/dim]")
    selected = await _show_model_picker(repl, pool)
    if selected:
        await activate_model(repl, selected)


async def _model_connect(repl: VeluneREPL, name: str) -> None:
    if not name:
        return await _model_discover(repl)
    registry = repl._require("runtime.model_registry", "model registry")
    if registry is None:
        return
    model = registry.get(name)
    if model is None:
        repl.console.print(f"[dim]'{name}' not in registry — discovering...[/dim]")
        try:
            await registry.refresh()
        except Exception as exc:
            repl.console.print(f"[red]Discovery failed:[/red] {exc}")
            return
        model = registry.get(name)
    if model is None:
        from velune.cli.rendering.error_panel import render_error
        from velune.core.errors.catalog import ModelNotFoundError

        repl.console.print(render_error(ModelNotFoundError(f"'{name}'")))
        return
    await activate_model(repl, model)


async def _model_use(repl: VeluneREPL, name: str) -> None:
    if not name:
        repl.console.print("[yellow]Usage: /model use <model-id>[/yellow]")
        return
    registry = repl._require("runtime.model_registry", "model registry")
    if registry is None:
        return
    model = registry.get(name)
    if model is None:
        from velune.cli.rendering.error_panel import render_error
        from velune.core.errors.catalog import ModelNotFoundError

        repl.console.print(render_error(ModelNotFoundError(f"'{name}'")))
        repl.console.print("[dim]→ Run [bold]/model discover[/bold] to refresh the registry.[/dim]")
        return
    await activate_model(repl, model)


async def _model_status(repl: VeluneREPL) -> None:
    from rich.panel import Panel

    if repl.active_model is None:
        restore_active_model(repl)
    if repl.active_model is None:
        repl.console.print(
            "[yellow]No active model.[/yellow] "
            "[dim]Use [bold]/model discover[/bold] or [bold]/model use <id>[/bold].[/dim]"
        )
        return
    m = repl.active_model
    reachable = "[dim]unknown[/dim]"
    try:
        provider_registry = repl.container.get("runtime.provider_registry")
        provider = provider_registry.get(m.provider_id)
        if provider is not None and hasattr(provider, "health_check"):
            ok = await provider.health_check()
            reachable = "[green]reachable[/green]" if ok else "[red]unreachable[/red]"
    except Exception:
        pass
    repl.console.print(
        Panel(
            f"[bold cyan]{m.model_id}[/bold cyan]\n"
            f"provider   {m.provider_id}\n"
            f"location   {'local' if m.is_local else 'cloud'}\n"
            f"context    {m.context_length:,} tokens\n"
            f"status     {reachable}",
            title="Active Model",
            border_style="dim",
        )
    )


async def _model_remove(repl: VeluneREPL, name: str) -> None:
    if not name:
        repl.console.print("[yellow]Usage: /model remove <model-id>[/yellow]")
        return
    registry = repl._require("runtime.model_registry", "model registry")
    if registry is None:
        return
    removed = registry.remove(name) if hasattr(registry, "remove") else False
    from velune.cli.model_prefs import clear_active_model, load_active_model

    pref = load_active_model()
    if pref and pref.model_id == name:
        clear_active_model()
        if repl.active_model and repl.active_model.model_id == name:
            repl.active_model = None
    if removed:
        repl.console.print(f"[green]Removed [cyan]{name}[/cyan] from the registry.[/green]")
    else:
        repl.console.print(
            f"[yellow]'{name}' was not in the registry.[/yellow] "
            "[dim](Use [bold]/delete[/bold] to remove an installed Ollama model.)[/dim]"
        )


async def _model_locate(repl: VeluneREPL) -> None:
    from velune.cli.dir_browser import browse_for_directory
    from velune.providers.ollama_locations import OllamaLocationRegistry
    from velune.providers.ollama_store import OllamaModelStore

    repl.console.print(
        "[dim]Browse to the folder that contains your Ollama models "
        "(the one with [bold]manifests/[/bold] and [bold]blobs/[/bold]). "
        "Use ← to go up to drives/volumes.[/dim]"
    )
    try:
        chosen = await browse_for_directory(
            title="Locate your Ollama model store",
            validate=OllamaModelStore.is_valid_root,
        )
    except Exception as exc:
        repl.console.print(f"[red]Could not open the browser:[/red] {exc}")
        return
    if chosen is None:
        repl.console.print("[dim]Cancelled.[/dim]")
        return

    result = OllamaLocationRegistry().add(chosen)
    if not result.ok:
        repl.console.print(f"[red]{result.message}[/red]")
        repl.console.print(
            "[dim]Tip: pick the directory that directly contains "
            "[bold]manifests/[/bold] and [bold]blobs/[/bold].[/dim]"
        )
        return

    repl.console.print(f"[green]{result.message}[/green]")
    try:
        found = OllamaModelStore(chosen).list_models()
        if found:
            names = ", ".join(m.name for m in found[:8])
            more = f" (+{len(found) - 8} more)" if len(found) > 8 else ""
            repl.console.print(f"[dim]Models here: {names}{more}[/dim]")
    except Exception:
        pass

    registry = repl._require("runtime.model_registry", "model registry")
    if registry is not None:
        repl.console.print("[dim]Refreshing model registry...[/dim]")
        try:
            await registry.refresh()
        except Exception as exc:
            repl.console.print(f"[yellow]Discovery refresh failed:[/yellow] {exc}")
            return
        if repl._completer is not None:
            repl._completer.set_model_ids([m.model_id for m in registry.list_all()])
        repl.console.print(
            "[dim]→ Run [bold]/models[/bold] to see them, or [bold]/model[/bold] to switch.[/dim]"
        )


async def _model_locations(repl: VeluneREPL, args: str) -> None:
    from velune.providers.ollama_locations import OllamaLocationRegistry

    reg = OllamaLocationRegistry()
    parts = args.split(None, 1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "add":
        if not rest:
            repl.console.print("[yellow]Usage: /model locations add <path>[/yellow]")
            return
        result = reg.add(rest)
        style = "green" if result.ok else "red"
        repl.console.print(f"[{style}]{result.message}[/{style}]")
        return

    from velune.cli.ui_components import create_table, print_header, print_notification

    if sub in ("remove", "rm", "delete"):
        if not rest:
            repl.console.print("[yellow]Usage: /model locations remove <path>[/yellow]")
            return
        ok = reg.remove(rest)
        if ok:
            print_notification(repl.console, f"Removed location: {rest}", type="success")
        else:
            print_notification(repl.console, f"Not a registered location: {rest}", type="warning")
        return

    roots = reg.resolve_roots()
    if not roots:
        print_notification(
            repl.console,
            "No model locations resolved. Run /model locate to register one.",
            type="info",
        )
        return

    table = create_table("Location", "Source", "Status")
    for rr in roots:
        if rr.disconnected:
            status = "[yellow]disconnected (device unavailable)[/yellow]"
        elif not rr.exists:
            status = "[dim]not present[/dim]"
        elif rr.is_valid:
            status = "[green]connected[/green]"
        else:
            status = "[red]not an Ollama store[/red]"
        table.add_row(str(rr.path), rr.source, status)

    print_header(repl.console, "Model Locations")
    repl.console.print(table)
    repl.console.print()

    disconnected = [rr for rr in roots if rr.disconnected]
    if disconnected:
        print_notification(
            repl.console,
            "Reconnect the drive(s) above and the models reappear automatically — no re-setup needed.",
            type="info",
        )


async def cmd_pull(repl: VeluneREPL, args: str) -> None:
    from velune.providers.ollama_manager import OllamaManager

    manager = OllamaManager()

    if not await manager.is_running():
        repl.console.print(
            "[red]Ollama is not running.[/red]\n[dim]Start it with: ollama serve[/dim]"
        )
        return

    if args.strip():
        success = await manager.pull_model(args.strip(), repl.console)
        if success:
            await _refresh_model_registry(repl)
    else:
        from velune.cli.pull_ui import run_pull_ui

        local_models = await manager.list_local_models()
        hardware = repl.container.get("runtime.hardware")
        ram_gb = float(hardware.total_ram_gb) if hardware else 16.0
        chosen = await run_pull_ui(local_models, ram_gb, repl.console)
        if chosen:
            if chosen in local_models:
                repl.console.print(f"[yellow]{chosen} is already installed.[/yellow]")
                return
            success = await manager.pull_model(chosen, repl.console)
            if success:
                await _refresh_model_registry(repl)


async def cmd_delete(repl: VeluneREPL, args: str) -> None:
    if not args.strip():
        repl.console.print("[yellow]Usage: /delete <model-id>[/yellow]")
        return
    from rich.prompt import Confirm

    from velune.providers.ollama_manager import OllamaManager

    model_id = args.strip()
    confirm = Confirm.ask(
        f"  Delete [cyan]{model_id}[/cyan] from Ollama? This cannot be undone.",
        default=False,
    )
    if not confirm:
        return
    manager = OllamaManager()
    if await manager.delete_model(model_id):
        repl.console.print(f"[green]Deleted: {model_id}[/green]")
        await _refresh_model_registry(repl)
    else:
        repl.console.print(f"[red]Failed to delete {model_id}[/red]")


async def cmd_bench(repl: VeluneREPL, args: str) -> None:
    """View or run empirical model capability benchmarks."""
    from pathlib import Path

    profile_path = Path.cwd() / ".velune" / "model_profiles.json"

    if args.strip() == "run" or not profile_path.exists():
        repl.console.print("[yellow]Running model capability scan & benchmarks...[/yellow]")
        model_registry = repl.container.get("runtime.model_registry")
        provider_registry = repl.container.get("runtime.provider_registry")

        if not model_registry or not provider_registry:
            repl.console.print("[red]Model/Provider registry is not available.[/red]")
            return

        models = model_registry.list_all()
        models_to_probe = [m for m in models if provider_registry.get(m.provider_id) is not None]

        if not models_to_probe:
            repl.console.print("[yellow]No models found/active to benchmark.[/yellow]")
            return

        from velune.cli.commands.models import _models_benchmark_async
        from velune.cli.context import CLIContext

        cli_ctx = CLIContext(
            workspace=Path.cwd(),
            config_path=None,
            verbose=False,
            runtime=repl.runtime,
        )

        await _models_benchmark_async(cli_ctx, model_registry, provider_registry, models_to_probe)
    else:
        try:
            import json
            from collections import namedtuple

            from velune.cli.commands.models import _display_benchmark_results
            from velune.cli.context import CLIContext
            from velune.core.types.model import ModelDescriptor

            ProbeResultMock = namedtuple("ProbeResultMock", ["score", "passed", "latency_ms"])

            data = json.loads(profile_path.read_text(encoding="utf-8"))
            if not data:
                repl.console.print(
                    "[yellow]No cached benchmark results found. Run /bench run to scan.[/yellow]"
                )
                return

            cli_ctx = CLIContext(
                workspace=Path.cwd(),
                config_path=None,
                verbose=False,
                runtime=repl.runtime,
            )

            benchmark_results = []
            for key, val in data.items():
                parts = key.split("/", 1)
                if len(parts) == 2:
                    prov_id, mod_id = parts
                else:
                    prov_id = "unknown"
                    mod_id = key

                probes = val.get("probes", {})
                if not probes:
                    continue

                model_desc = ModelDescriptor(
                    model_id=mod_id,
                    provider_id=prov_id,
                    display_name=mod_id,
                    context_length=8192,
                    capabilities=None,
                )

                coding_raw = probes.get("coding", {})
                reasoning_raw = probes.get("reasoning", {})
                instruction_raw = probes.get("instruction", {})

                coding = ProbeResultMock(
                    score=coding_raw.get("score", 0.0),
                    passed=coding_raw.get("passed", False),
                    latency_ms=coding_raw.get("latency_ms", -1.0),
                )
                reasoning = ProbeResultMock(
                    score=reasoning_raw.get("score", 0.0),
                    passed=reasoning_raw.get("passed", False),
                    latency_ms=reasoning_raw.get("latency_ms", -1.0),
                )
                instruction = ProbeResultMock(
                    score=instruction_raw.get("score", 0.0),
                    passed=instruction_raw.get("passed", False),
                    latency_ms=instruction_raw.get("latency_ms", -1.0),
                )

                latencies = [
                    lat
                    for lat in [coding.latency_ms, reasoning.latency_ms, instruction.latency_ms]
                    if lat > 0
                ]
                avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
                speed_score = max(0.0, 1.0 - (avg_latency / 3000.0))

                benchmark_results.append(
                    {
                        "model": model_desc,
                        "coding": coding,
                        "reasoning": reasoning,
                        "instruction": instruction,
                        "speed_score": speed_score,
                        "avg_latency_ms": avg_latency,
                    }
                )

            _display_benchmark_results(cli_ctx, benchmark_results)
        except Exception as e:
            repl.console.print(f"[red]Failed to display benchmarks: {e}[/red]")


async def _refresh_model_registry(repl: VeluneREPL) -> None:
    model_registry = repl.container.get("runtime.model_registry")
    if not model_registry:
        return
    try:
        await model_registry.refresh()
        count = len(model_registry.list_all())
        repl.console.print(f"[dim]Model registry refreshed: {count} models available.[/dim]")
    except Exception:
        pass
