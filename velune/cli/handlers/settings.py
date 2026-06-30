"""Settings slash command handlers: /config /hooks /approve /doctor /sandbox."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.settings")


async def cmd_config(repl: VeluneREPL, args: str) -> None:
    """Show current system configuration settings."""
    import logging as _logging
    from pathlib import Path

    from velune.cli.ui_components import create_table, print_header

    config = repl.runtime.config

    table = create_table("Setting", "Value")

    table.add_row("Config Path", str(repl.runtime.config_path or "default (memory)"))
    table.add_row("Workspace Root", str(repl.runtime.workspace or Path.cwd()))
    verbose = _logging.getLogger("velune").getEffectiveLevel() <= _logging.DEBUG
    table.add_row("Log Level", "DEBUG" if verbose else "INFO")

    if hasattr(config, "model_dump"):
        dump = config.model_dump()
    elif hasattr(config, "dict"):
        dump = config.dict()
    else:
        dump = {}

    def flatten_dict(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            name = f"{prefix}{k}"
            if isinstance(v, dict):
                flatten_dict(v, prefix=f"{name}.")
            else:
                table.add_row(name, str(v))

    flatten_dict(dump)

    print_header(repl.console, "Velune System Configuration")
    repl.console.print(table)
    repl.console.print()


async def cmd_hooks(repl: VeluneREPL, args: str) -> None:
    """List active hook bindings from project + user config."""
    from velune.cli.ui_components import create_table, print_header, print_notification

    rows = repl._hook_dispatcher.summary()
    if not rows:
        print_notification(
            repl.console,
            "No hooks configured. Create .velune/hooks.json or ~/.velune/hooks.json to add lifecycle hooks.",
            type="info",
        )
        return

    table = create_table("Event", "Matcher", "Command", "Timeout", "If Condition")

    for row in rows:
        table.add_row(
            row.get("event", ""),
            row.get("matcher", "*") or "*",
            row.get("command", ""),
            f"{row.get('timeout', 10)}s",
            row.get("if", "") or "—",
        )

    print_header(repl.console, "Lifecycle Hooks")
    repl.console.print(table)
    repl.console.print(
        f"\n[dim]{len(rows)} hook(s) loaded. "
        "Use [bold]/hooks[/bold] to see updates (cache reloads automatically).[/dim]"
    )


async def cmd_approve(repl: VeluneREPL, args: str) -> None:
    """Set the tool/command approval mode for this session."""
    from velune.tools.safety import ApprovalMode

    sub = args.strip().lower()
    if not sub:
        modes = ", ".join(m.value for m in ApprovalMode)
        repl.console.print(
            f"[cyan]Current approval mode:[/cyan] [bold]{repl._approval_mode.value}[/bold]\n"
            f"[dim]Usage: /approve [{modes}][/dim]\n"
            f"\n"
            f"  [bold]safe[/bold]   — known read-only commands run without prompting\n"
            f"  [bold]ask[/bold]    — all tool/shell calls require confirmation  [dim](default)[/dim]\n"
            f"  [bold]block[/bold]  — all shell tool calls are rejected"
        )
        return

    try:
        new_mode = ApprovalMode(sub)
    except ValueError:
        modes = " | ".join(m.value for m in ApprovalMode)
        repl.console.print(f"[red]Unknown mode: {sub!r}[/red]  [dim]Choose: {modes}[/dim]")
        return

    repl._approval_mode = new_mode
    style = {"safe": "green", "ask": "yellow", "block": "red"}.get(new_mode.value, "white")
    repl.console.print(f"[{style}]Approval mode set to:[/{style}] [bold]{new_mode.value}[/bold]")


async def cmd_doctor(repl: VeluneREPL, args: str) -> None:
    from velune.cli.commands.doctor import (
        _check_anthropic_api_key,
        _check_config,
        _check_core_dependencies,
        _check_git,
        _check_gpu,
        _check_groq,
        _check_lm_studio,
        _check_model_benchmarks,
        _check_ollama_connectivity,
        _check_ollama_models,
        _check_openai_api_key,
        _check_python_version,
        _check_qdrant,
        _check_sqlite,
        _check_treesitter,
        _check_velune_dir,
        _check_vram,
        _render_results,
    )

    checks = [
        _check_python_version,
        _check_core_dependencies,
        _check_ollama_connectivity,
        _check_ollama_models,
        _check_lm_studio,
        _check_openai_api_key,
        _check_anthropic_api_key,
        _check_groq,
        _check_velune_dir,
        _check_sqlite,
        _check_qdrant,
        _check_config,
        _check_treesitter,
        _check_git,
        _check_gpu,
        _check_vram,
        _check_model_benchmarks,
    ]
    results = []
    with repl.console.status("[cyan]Running health checks...[/cyan]"):
        for check_fn in checks:
            try:
                results.append(check_fn())
            except Exception as e:
                results.append(
                    {
                        "name": check_fn.__name__.replace("_check_", "").replace("_", " ").title(),
                        "status": "error",
                        "message": str(e),
                    }
                )
    _render_results(results)
    failures = sum(1 for r in results if r["status"] == "fail")
    if failures:
        repl.console.print(
            f"[red]{failures} check(s) failed.[/red]  "
            "[dim]Run [cyan]velune doctor --fix[/cyan] to attempt automatic fixes.[/dim]"
        )
        repl.console.print("[dim]→ /providers to add or fix API keys  ·  /settings to reconfigure[/dim]")
    else:
        repl.console.print("[green]All checks passed.[/green]")
        repl.console.print("[dim]→ /models to see available models  ·  /run <task> to start working[/dim]")


async def cmd_sandbox(repl: VeluneREPL, args: str) -> None:
    """Show current sandbox type and status, or start Docker sandbox."""
    from pathlib import Path

    sub = args.strip().lower()

    workspace_raw = repl.container.get("runtime.workspace")
    workspace = Path(workspace_raw) if workspace_raw else Path.cwd()

    if sub in ("docker", "start"):
        from velune.execution.docker_sandbox import DockerSandbox, DockerUnavailableError

        try:
            sb = DockerSandbox.for_workspace(workspace)
            with repl.console.status("[cyan]Starting Docker sandbox…[/cyan]"):
                sb.start()
            repl.console.print(
                f"[green]Docker sandbox started[/green]\n"
                f"  Container: [bold]{sb.session_id}[/bold]\n"
                f"  Image:     [dim]{sb.image}[/dim]\n"
                f"  Workspace: [dim]{workspace} → /workspace[/dim]\n\n"
                f"[dim]This sandbox is standalone. To route agent execution through Docker,\n"
                f"set [bold]execution.docker_sandbox = true[/bold] in [bold]velune.toml[/bold].[/dim]"
            )
        except DockerUnavailableError as exc:
            repl.console.print(
                f"[red]Docker unavailable:[/red] {exc}\n"
                "[dim]Install Docker Desktop and ensure the daemon is running.[/dim]"
            )
        except Exception as exc:
            repl.console.print(f"[red]Sandbox start failed:[/red] {exc}")
        return

    # Default: show status info
    try:
        from velune.execution.docker_sandbox import DockerSandbox

        test_sb = DockerSandbox.for_workspace(workspace)
        test_client = test_sb._get_docker_client()
        docker_info = test_client.version()
        docker_version = docker_info.get("Version", "unknown")
        docker_ok = True
    except Exception:
        docker_version = "unavailable"
        docker_ok = False

    try:
        from velune.kernel.config import ConfigLoader

        cfg = ConfigLoader(workspace / "velune.toml").load()
        docker_configured = getattr(getattr(cfg, "execution", None), "docker_sandbox", False)
        docker_image = getattr(getattr(cfg, "execution", None), "docker_image", "python:3.12-slim")
    except Exception:
        docker_configured = False
        docker_image = "python:3.12-slim"

    active = "Docker" if docker_configured and docker_ok else "Subprocess"
    docker_status = (
        f"[green]available v{docker_version}[/green]" if docker_ok else "[red]unavailable[/red]"
    )

    repl.console.print(
        f"\n[bold cyan]Sandbox Status[/bold cyan]\n"
        f"  Active mode:   [bold]{active}[/bold]\n"
        f"  Docker daemon: {docker_status}\n"
        f"  Docker image:  [dim]{docker_image}[/dim]\n"
        f"  Configured:    [bold]{'docker' if docker_configured else 'subprocess'}[/bold] "
        f"[dim](execution.docker_sandbox in velune.toml)[/dim]\n\n"
        f"[dim]Run [bold]/sandbox docker[/bold] to test-start a Docker sandbox.[/dim]\n"
        f"[dim]Set [bold]execution.docker_sandbox = true[/bold] in velune.toml to route all agent execution through Docker.[/dim]"
    )


# ── Settings Interface TUI ───────────────────────────────────────────────────

SETTINGS_DEFS = {
    "Appearance": [
        {
            "name": "Theme",
            "desc": "UI Theme color palette",
            "type": "choice",
            "choices": ["dark", "light", "monokai", "nord", "dracula"],
            "section": "appearance",
            "key": "theme",
            "default": "dark",
        },
        {
            "name": "Show Status Bar",
            "desc": "Display the active status bar at prompt bottom",
            "type": "bool",
            "section": "appearance",
            "key": "show_status_bar",
            "default": True,
        },
        {
            "name": "Font Size",
            "desc": "Terminal window display font size",
            "type": "int",
            "section": "appearance",
            "key": "font_size",
            "default": 12,
        },
    ],
    "Providers": [
        {
            "name": "Default Provider",
            "desc": "Default LLM API provider",
            "type": "choice",
            "choices": ["openai", "anthropic", "gemini", "ollama"],
            "section": "providers",
            "key": "default_provider",
            "default": "openai",
        },
        {
            "name": "OpenAI Base URL",
            "desc": "Custom endpoint URL for OpenAI provider",
            "type": "string",
            "section": "providers",
            "key": "openai_base_url",
            "default": "https://api.openai.com/v1",
        },
        {
            "name": "Anthropic Base URL",
            "desc": "Custom endpoint URL for Anthropic provider",
            "type": "string",
            "section": "providers",
            "key": "anthropic_base_url",
            "default": "https://api.anthropic.com/v1",
        },
    ],
    "Models": [
        {
            "name": "Active Model ID",
            "desc": "The currently active model used for tasks",
            "type": "string",
            "section": "models",
            "key": "active_model_id",
            "default": "gpt-4o",
        },
        {
            "name": "Speed Tier Preference",
            "desc": "Preferred speed tier for model choices",
            "type": "choice",
            "choices": ["fast", "medium", "slow"],
            "section": "models",
            "key": "speed_tier_preference",
            "default": "medium",
        },
    ],
    "Memory": [
        {
            "name": "SQLite Connection Pool Size",
            "desc": "Max size of concurrent SQLite connections",
            "type": "int",
            "section": "memory",
            "key": "sqlite_pool_size",
            "default": 5,
        },
        {
            "name": "Vector Index Type",
            "desc": "Vector database store choice",
            "type": "choice",
            "choices": ["lancedb", "qdrant", "chroma"],
            "section": "memory",
            "key": "vector_index_type",
            "default": "lancedb",
        },
    ],
    "Workspace": [
        {
            "name": "Auto-Index on Open",
            "desc": "Index workspace automatically when directory opens",
            "type": "bool",
            "section": "workspace",
            "key": "auto_index",
            "default": True,
        },
        {
            "name": "Workspace Trust Level",
            "desc": "Trust boundary execution constraints",
            "type": "choice",
            "choices": ["trusted", "restricted", "sandboxed"],
            "section": "workspace",
            "key": "trust_level",
            "default": "trusted",
        },
    ],
    "MCP": [
        {
            "name": "MCP Enabled",
            "desc": "Enable Model Context Protocol support",
            "type": "bool",
            "section": "mcp",
            "key": "enabled",
            "default": True,
        },
        {
            "name": "Registry Config Path",
            "desc": "Absolute path to mcp_servers.json configuration",
            "type": "string",
            "section": "mcp",
            "key": "registry_path",
            "default": "~/.velune/mcp_servers.json",
        },
    ],
    "Performance": [
        {
            "name": "Max Concurrent Threads",
            "desc": "Max worker threads for concurrent tasks",
            "type": "int",
            "section": "performance",
            "key": "max_threads",
            "default": 4,
        },
        {
            "name": "Rate Limit (RPM)",
            "desc": "Maximum request rate per minute",
            "type": "int",
            "section": "performance",
            "key": "rate_limit_rpm",
            "default": 60,
        },
        {
            "name": "Retrieval Context Depth",
            "desc": "Depth of retrieved code context elements",
            "type": "int",
            "section": "performance",
            "key": "context_depth",
            "default": 20,
        },
    ],
    "Security": [
        {
            "name": "Docker Sandbox Enabled",
            "desc": "Run untrusted build/commands inside isolated Docker sandbox",
            "type": "bool",
            "section": "security",
            "key": "docker_sandbox",
            "default": False,
        },
        {
            "name": "Command Approval Level",
            "desc": "User approval requirement before running commands",
            "type": "choice",
            "choices": ["safe", "ask", "block"],
            "section": "security",
            "key": "approval_level",
            "default": "ask",
        },
    ],
    "Telemetry": [
        {
            "name": "Anonymous Telemetry",
            "desc": "Opt-in to share usage statistics anonymously",
            "type": "bool",
            "section": "telemetry",
            "key": "enabled",
            "default": True,
        }
    ],
    "Experimental": [
        {
            "name": "Smart Model Routing",
            "desc": "Route complex prompts to larger models automatically",
            "type": "bool",
            "section": "experimental",
            "key": "smart_routing",
            "default": False,
        },
        {
            "name": "Code Summarizer View",
            "desc": "Surface code summaries automatically in background",
            "type": "bool",
            "section": "experimental",
            "key": "code_summarizer",
            "default": False,
        },
    ],
}


def get_setting_value(repl: VeluneREPL, section: str, key: str, default: Any) -> Any:
    # Try reading from memory/runtime config
    try:
        config = repl.container.get("runtime.config")
        sec_obj = getattr(config, section, None)
        if sec_obj is not None:
            val = getattr(sec_obj, key, None)
            if val is not None:
                return val
    except Exception:
        pass

    # Try reading from TOML directly
    try:
        workspace = Path(repl.container.get("runtime.workspace"))
        config_path = repl.container.get("runtime.config_path") or (workspace / "velune.toml")
        if config_path.exists():
            import toml

            data = toml.load(config_path)
            val = data.get(section, {}).get(key)
            if val is not None:
                return val
    except Exception:
        pass

    return default


def save_setting_to_toml(repl: VeluneREPL, section: str, key: str, value: Any) -> None:
    try:
        import toml

        workspace = Path(repl.container.get("runtime.workspace"))
        config_path = repl.container.get("runtime.config_path") or (workspace / "velune.toml")
        config_path = Path(config_path)
        data = toml.load(config_path) if config_path.exists() else {}
        data.setdefault(section, {})[key] = value
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as fh:
            toml.dump(data, fh)
    except Exception as exc:
        _log.debug("Could not save setting to velune.toml: %s", exc)


def _update_runtime_config(repl: VeluneREPL, section: str, key: str, value: Any) -> None:
    try:
        config = repl.container.get("runtime.config")
        sec_obj = getattr(config, section, None)
        if sec_obj is not None:
            setattr(sec_obj, key, value)
    except Exception:
        pass

    # Custom triggers
    if section == "models" and key == "active_model_id":
        try:
            registry = repl.container.get("runtime.model_registry")
            model = registry.get(value)
            if model:
                repl.active_model = model
        except Exception:
            pass


async def cmd_settings(repl: VeluneREPL, args: str) -> None:
    """Settings TUI slash command: /settings."""
    await _show_settings_tui(repl)


async def _show_settings_tui(repl: VeluneREPL) -> None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    categories = [
        "Appearance",
        "Providers",
        "Models",
        "Memory",
        "Workspace",
        "MCP",
        "Performance",
        "Security",
        "Telemetry",
        "Experimental",
    ]

    settings_cache = {}
    for cat in categories:
        settings_cache[cat] = []
        for s_def in SETTINGS_DEFS[cat]:
            curr_val = get_setting_value(repl, s_def["section"], s_def["key"], s_def["default"])
            settings_cache[cat].append({**s_def, "value": curr_val})

    state = {
        "mode": "categories",
        "selected_category_idx": 0,
        "selected_setting_idx": 0,
        "selected_choice_idx": 0,
        "edit_text_value": "",
        "status_message": "Use arrow keys to navigate. Enter to select. Esc to exit.",
    }

    def _render_tui() -> FormattedText:
        lines = []
        lines.append(("bold fg:ansicyan", "  VELUNE SYSTEM SETTINGS\n"))
        lines.append(("fg:ansidarkgray", "  " + "—" * 70 + "\n"))

        if state["mode"] == "categories":
            lines.append(("", "\n  Select Category:\n\n"))
            for i, cat in enumerate(categories):
                is_sel = i == state["selected_category_idx"]
                prefix = "❯ " if is_sel else "  "
                style = "bold fg:ansiyellow" if is_sel else "fg:ansiwhite"
                lines.append((style, f"  {prefix}{cat}\n"))
            lines.append(("", "\n"))

        elif state["mode"] == "settings":
            cat = categories[state["selected_category_idx"]]
            lines.append(("", "\n  Category: "))
            lines.append(("bold fg:ansiyellow", f"{cat}"))
            lines.append(("", " settings:\n\n"))

            cat_settings = settings_cache[cat]
            for i, s in enumerate(cat_settings):
                is_sel = i == state["selected_setting_idx"]
                prefix = "❯ " if is_sel else "  "
                style = "bold fg:ansicyan" if is_sel else "fg:ansiwhite"

                val = s["value"]
                if s["type"] == "bool":
                    val_str = "True" if val else "False"
                    val_style = "fg:ansigreen" if val else "fg:ansired"
                else:
                    val_str = str(val)
                    val_style = "fg:ansicyan"

                if is_sel:
                    lines.append(("bold fg:ansicyan", f"  {prefix}{s['name']}: "))
                    lines.append((val_style + " bold", f"{val_str}"))
                    lines.append(("fg:ansidarkgray", f"  — {s['desc']}\n"))
                else:
                    lines.append(("fg:ansiwhite", f"  {prefix}{s['name']}: "))
                    lines.append((val_style, f"{val_str}"))
                    lines.append(("fg:ansidarkgray", f"  — {s['desc']}\n"))

            is_back_sel = state["selected_setting_idx"] == len(cat_settings)
            prefix = "❯ " if is_back_sel else "  "
            back_style = "bold fg:ansiyellow" if is_back_sel else "fg:ansidarkgray"
            lines.append((back_style, f"\n  {prefix}[Back to Categories]\n"))

        elif state["mode"] == "choices":
            cat = categories[state["selected_category_idx"]]
            setting = settings_cache[cat][state["selected_setting_idx"]]
            lines.append(("", "\n  Select value for "))
            lines.append(("bold fg:ansicyan", f"{setting['name']}"))
            lines.append(("", ":\n\n"))

            for i, choice in enumerate(setting["choices"]):
                is_sel = i == state["selected_choice_idx"]
                prefix = "❯ " if is_sel else "  "
                style = "bold fg:ansiyellow" if is_sel else "fg:ansiwhite"

                bullet = "• " if str(setting["value"]) == str(choice) else "  "
                lines.append((style, f"  {prefix}{bullet}{choice}\n"))

            lines.append(("", "\n"))

        elif state["mode"] == "text_input":
            cat = categories[state["selected_category_idx"]]
            setting = settings_cache[cat][state["selected_setting_idx"]]
            lines.append(("", "\n  Editing setting: "))
            lines.append(("bold fg:ansicyan", f"{setting['name']}\n"))
            lines.append(("fg:ansidarkgray", f"  Description: {setting['desc']}\n\n"))

            lines.append(("bold fg:ansiwhite", "  Enter new value: "))
            lines.append(("fg:ansiyellow bold", state["edit_text_value"]))
            lines.append(("fg:ansiyellow blink", "█"))
            lines.append(("", "\n\n  [Press Enter to Save · Esc to Cancel]\n"))

        lines.append(("fg:ansidarkgray", "  " + "—" * 70 + "\n"))
        lines.append(("fg:ansidarkgray", f"  {state['status_message']}\n"))
        return FormattedText(lines)

    kb = KeyBindings()

    @kb.add("up")
    def _up(event) -> None:
        if state["mode"] == "categories":
            state["selected_category_idx"] = (state["selected_category_idx"] - 1) % len(categories)
        elif state["mode"] == "settings":
            cat = categories[state["selected_category_idx"]]
            total = len(settings_cache[cat]) + 1
            state["selected_setting_idx"] = (state["selected_setting_idx"] - 1) % total
        elif state["mode"] == "choices":
            cat = categories[state["selected_category_idx"]]
            setting = settings_cache[cat][state["selected_setting_idx"]]
            state["selected_choice_idx"] = (state["selected_choice_idx"] - 1) % len(
                setting["choices"]
            )

    @kb.add("down")
    def _down(event) -> None:
        if state["mode"] == "categories":
            state["selected_category_idx"] = (state["selected_category_idx"] + 1) % len(categories)
        elif state["mode"] == "settings":
            cat = categories[state["selected_category_idx"]]
            total = len(settings_cache[cat]) + 1
            state["selected_setting_idx"] = (state["selected_setting_idx"] + 1) % total
        elif state["mode"] == "choices":
            cat = categories[state["selected_category_idx"]]
            setting = settings_cache[cat][state["selected_setting_idx"]]
            state["selected_choice_idx"] = (state["selected_choice_idx"] + 1) % len(
                setting["choices"]
            )

    @kb.add("enter")
    def _enter(event) -> None:
        if state["mode"] == "categories":
            state["mode"] = "settings"
            state["selected_setting_idx"] = 0
            state["status_message"] = "Arrow keys to select. Enter to edit. Backspace to go back."
        elif state["mode"] == "settings":
            cat = categories[state["selected_category_idx"]]
            cat_settings = settings_cache[cat]
            if state["selected_setting_idx"] == len(cat_settings):
                state["mode"] = "categories"
                state["status_message"] = "Arrow keys to select. Enter to enter category."
            else:
                s = cat_settings[state["selected_setting_idx"]]
                if s["type"] == "bool":
                    new_val = not s["value"]
                    s["value"] = new_val
                    save_setting_to_toml(repl, s["section"], s["key"], new_val)
                    _update_runtime_config(repl, s["section"], s["key"], new_val)
                    state["status_message"] = f"Toggled {s['name']} to {new_val}"
                elif s["type"] == "choice":
                    state["mode"] = "choices"
                    try:
                        state["selected_choice_idx"] = s["choices"].index(s["value"])
                    except Exception:
                        state["selected_choice_idx"] = 0
                    state["status_message"] = "Arrow keys to select. Enter to save."
                elif s["type"] in ("int", "string"):
                    state["mode"] = "text_input"
                    state["edit_text_value"] = str(s["value"])
                    state["status_message"] = "Type new value. Enter to save. Esc to cancel."
        elif state["mode"] == "choices":
            cat = categories[state["selected_category_idx"]]
            s = settings_cache[cat][state["selected_setting_idx"]]
            choice = s["choices"][state["selected_choice_idx"]]
            s["value"] = choice
            save_setting_to_toml(repl, s["section"], s["key"], choice)
            _update_runtime_config(repl, s["section"], s["key"], choice)
            state["mode"] = "settings"
            state["status_message"] = f"Set {s['name']} to {choice}"
        elif state["mode"] == "text_input":
            cat = categories[state["selected_category_idx"]]
            s = settings_cache[cat][state["selected_setting_idx"]]
            val_raw = state["edit_text_value"]
            if s["type"] == "int":
                try:
                    val = int(val_raw)
                except ValueError:
                    state["status_message"] = "Error: Invalid integer."
                    return
            else:
                val = val_raw
            s["value"] = val
            save_setting_to_toml(repl, s["section"], s["key"], val)
            _update_runtime_config(repl, s["section"], s["key"], val)
            state["mode"] = "settings"
            state["status_message"] = f"Saved {s['name']} value: {val}"

    @kb.add("f")
    def _toggle_f(event) -> None:
        if state["mode"] == "settings":
            cat = categories[state["selected_category_idx"]]
            cat_settings = settings_cache[cat]
            if state["selected_setting_idx"] < len(cat_settings):
                s = cat_settings[state["selected_setting_idx"]]
                if s["type"] == "bool":
                    new_val = not s["value"]
                    s["value"] = new_val
                    save_setting_to_toml(repl, s["section"], s["key"], new_val)
                    _update_runtime_config(repl, s["section"], s["key"], new_val)
                    state["status_message"] = f"Toggled {s['name']} to {new_val}"

    @kb.add("backspace")
    def _backspace(event) -> None:
        if state["mode"] == "text_input":
            state["edit_text_value"] = state["edit_text_value"][:-1]
        elif state["mode"] == "settings":
            state["mode"] = "categories"
            state["status_message"] = "Arrow keys to select. Enter to enter category."
        elif state["mode"] == "choices":
            state["mode"] = "settings"
            state["status_message"] = "Arrow keys to select. Enter to edit."

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event) -> None:
        if state["mode"] == "text_input":
            state["mode"] = "settings"
            state["status_message"] = "Cancelled edit."
        elif state["mode"] == "choices":
            state["mode"] = "settings"
            state["status_message"] = "Cancelled selection."
        elif state["mode"] == "settings":
            state["mode"] = "categories"
            state["status_message"] = "Arrow keys to select."
        else:
            event.app.exit()

    @kb.add("<any>")
    def _type(event) -> None:
        if state["mode"] == "text_input":
            ch = event.data
            if ch and ch.isprintable():
                state["edit_text_value"] += ch

    app = Application(
        layout=Layout(
            Window(
                content=FormattedTextControl(_render_tui, focusable=True),
            )
        ),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )

    await app.run_async()
