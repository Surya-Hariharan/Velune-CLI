"""Settings slash command handlers: /config /hooks /doctor /sandbox."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.table import Table

from velune.cli import design

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.settings")


async def cmd_config(repl: VeluneREPL, args: str) -> None:
    """Show current system configuration settings."""
    import logging as _logging
    from pathlib import Path

    from velune.cli import ui

    config = repl.runtime.config

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Setting", "Value"):
        table.add_column(col, style=design.MUTED)

    table.add_row("Config Path", str(repl.runtime.config_path or "default (memory)"))
    table.add_row("Workspace Root", str(repl.runtime.workspace or Path.cwd()))
    verbose = _logging.getLogger("velune").getEffectiveLevel() <= _logging.DEBUG
    table.add_row("Log Level", "DEBUG" if verbose else "INFO")

    if hasattr(config, "model_dump"):
        dump = config.model_dump()
    elif hasattr(config, "dict"):
        dump = config.dict()  # type: ignore
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

    repl.console.print(ui.header("Velune System Configuration"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print()


async def cmd_hooks(repl: VeluneREPL, args: str) -> None:
    """List active hook bindings from project + user config."""
    from velune.cli import ui

    rows = repl._hook_dispatcher.summary()
    if not rows:
        repl.console.print(
            ui.notification(
                "No hooks configured. Create .velune/hooks.json or "
                "~/.velune/hooks.json to add lifecycle hooks.",
                kind="info",
            )
        )
        return

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Event", "Matcher", "Command", "Timeout", "If Condition"):
        table.add_column(col, style=design.MUTED)

    for row in rows:
        table.add_row(
            row.get("event", ""),
            row.get("matcher", "*") or "*",
            row.get("command", ""),
            f"{row.get('timeout', 10)}s",
            row.get("if", "") or "—",
        )

    repl.console.print(ui.header("Lifecycle Hooks"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print(
        f"\n[dim]{len(rows)} hook(s) loaded. "
        "Use [bold]/hooks[/bold] to see updates (cache reloads automatically).[/dim]"
    )


#: Accessibility modifier names accepted by ``/theme``, with their aliases.
#: These are display preferences, not themes — see ``ThemeConfig``.
_ACCESSIBILITY_OPTIONS: frozenset[str] = frozenset(
    {"colorblind", "cb", "motion", "animation", "animations"}
)


def _is_accessibility_option(name: str) -> bool:
    return name in _ACCESSIBILITY_OPTIONS


async def cmd_theme(repl: VeluneREPL, args: str) -> None:
    """Switch the colour theme, or toggle an accessibility modifier.

    Grammar, in the order it is checked:

    * ``/theme <theme-id>``  — switch to that theme
    * ``/theme colorblind|motion [on|off]`` — accessibility modifiers, which
      are *not* themes (Part 2D): they layer on top of whichever theme is
      active and survive a theme change
    * ``/theme`` / ``/theme status`` — current state

    In the fullscreen REPL a bare ``/theme`` never reaches this handler at
    all: the theme palette binds Enter eagerly and applies the selection
    in-place (see ``cli/theme_palette.py``). This path is what serves
    ``--plain`` mode, a piped/scripted session, and an explicit id.
    """
    from velune.cli import design, themes

    sub_raw = args.strip()
    first = sub_raw.split()[0].lower() if sub_raw else ""

    # A recognised theme id wins over everything else. Checked before the
    # accessibility table so a future theme named e.g. "motion" could not be
    # shadowed silently — the registry is the authority on what is a theme.
    if first and themes.get(first) is not None:
        theme = themes.get(first)
        assert theme is not None  # narrowed by the guard above
        repl._apply_theme_choice(theme)
        repl.console.print(
            f"[cyan]Theme set to[/cyan] [bold]{theme.name}[/bold]. "
            f"[dim]Saved to velune.toml — applies to this and future sessions.[/dim]"
        )
        return

    if first and first not in ("status",) and not _is_accessibility_option(first):
        known = ", ".join(t.id for t in themes.list_themes())
        repl.console.print(
            f"[red]Unknown theme or option: {first!r}[/red]\n"
            f"[dim]Themes: {known}  ·  Options: colorblind | motion[/dim]"
        )
        return

    # name -> (aliases, config key, getter, setter, label)
    options = {
        "colorblind": (
            ("cb",),
            "colorblind_mode",
            design.is_colorblind_mode,
            design.set_colorblind_mode,
            "Colorblind-safe severity palette",
        ),
        "motion": (
            ("animation", "animations"),
            "reduced_motion",
            lambda: not design.reduced_motion_enabled(),
            lambda v: design.set_reduced_motion(not v),
            "Spinner animation",
        ),
    }

    sub = sub_raw.lower()

    if sub in ("", "status"):
        active = design.active_theme()
        lines = [f"[cyan]Theme:[/cyan] [bold]{active.name}[/bold] [dim]({active.id})[/dim]"]
        lines += [
            f"[cyan]{label}:[/cyan] [bold]{'on' if getter() else 'off'}[/bold]"
            for _, _, getter, _, label in options.values()
        ]
        known = ", ".join(t.id for t in themes.list_themes())
        repl.console.print(
            "\n".join(lines)
            + f"\n[dim]Themes: {known}[/dim]"
            + "\n[dim]Usage: /theme <theme-id> | /theme <colorblind|motion> [on|off][/dim]"
        )
        return

    parts = sub.split()
    name = parts[0]
    resolved = next(
        (key for key, (aliases, *_rest) in options.items() if name == key or name in aliases),
        None,
    )
    if resolved is None:
        repl.console.print(
            f"[red]Unknown /theme option: {name!r}[/red]  [dim]Try: colorblind | motion[/dim]"
        )
        return

    _aliases, config_key, getter, setter, label = options[resolved]

    if len(parts) > 1:
        if parts[1] in ("on", "true", "1", "enable"):
            new_val = True
        elif parts[1] in ("off", "false", "0", "disable"):
            new_val = False
        else:
            repl.console.print(f"[red]Unknown value: {parts[1]!r}[/red]  [dim]Use on|off[/dim]")
            return
    else:
        new_val = not getter()

    setter(new_val)
    save_setting_to_toml(repl, "display", config_key, new_val)
    _update_runtime_config(repl, "display", config_key, new_val)

    state = "enabled" if new_val else "disabled"
    repl.console.print(
        f"[cyan]{label} {state}.[/cyan] "
        f"[dim]Saved to velune.toml — applies to this and future sessions.[/dim]"
    )


async def cmd_crashreports(repl: VeluneREPL, args: str) -> None:
    """Show or toggle opt-in local crash reporting.

    Off by default (see "Zero telemetry" in SECURITY.md). Enabling this never
    transmits anything anywhere — it only writes a redacted JSON snapshot of
    an unhandled crash (exception, traceback, versions; no local variables,
    no prompts/conversation content) to ~/.velune/crash_reports/, for the
    user's own diagnosis or to attach to a GitHub issue themselves.
    """
    from velune.cli.crash_reporter import CRASH_REPORT_DIR, is_enabled

    sub = args.strip().lower()

    if sub in ("", "status"):
        state = "on" if is_enabled() else "off"
        repl.console.print(
            f"[cyan]Local crash reporting:[/cyan] [bold]{state}[/bold]\n"
            f"[dim]Nothing is ever transmitted anywhere — reports are written locally to "
            f"{CRASH_REPORT_DIR} only. Usage: /crashreports [on|off][/dim]"
        )
        return

    if sub in ("on", "true", "1", "enable"):
        new_val = True
    elif sub in ("off", "false", "0", "disable"):
        new_val = False
    else:
        repl.console.print(f"[red]Unknown value: {sub!r}[/red]  [dim]Use on|off[/dim]")
        return

    save_setting_to_toml(repl, "telemetry", "crash_reports_enabled", new_val)
    _update_runtime_config(repl, "telemetry", "crash_reports_enabled", new_val)

    state = "enabled" if new_val else "disabled"
    detail = (
        f"A redacted crash snapshot will be written locally to {CRASH_REPORT_DIR} "
        "if Velune ever crashes unhandled. Nothing is transmitted anywhere."
        if new_val
        else "No crash data will be written."
    )
    repl.console.print(
        f"[cyan]Local crash reporting {state}.[/cyan] [dim]{detail} Saved to velune.toml.[/dim]"
    )


async def cmd_doctor(repl: VeluneREPL, args: str) -> None:
    """Run the same health checks as ``velune doctor`` and render them in the REPL."""
    from velune.cli.commands.doctor import (
        _render_results,
        _render_verdict,
        _run_checks,
        _verdict,
        repl_checks,
    )

    checks = repl_checks()

    # The checks do blocking network/subprocess work: run them off the event
    # loop so the full-screen UI keeps repainting and accepting input.
    with repl.console.status("[cyan]Running health checks...[/cyan]"):
        results = await asyncio.to_thread(_run_checks, checks)
    # Render through the app's own console. The module-level console in
    # commands/doctor.py writes straight to the real stdout, which tears the
    # full-screen display apart.
    _render_results(results, repl.console)
    verdict = _verdict(results)
    _render_verdict(verdict, repl.console)
    if verdict["core_healthy"]:
        repl.console.print(
            "[dim]→ /model to see available models  ·  /council <task> to start working[/dim]"
        )
    if verdict["optional_attention"]:
        repl.console.print(
            "[dim]→ /connect to add or fix API keys  ·  /config to see the settings[/dim]"
        )


async def cmd_sandbox(repl: VeluneREPL, args: str) -> None:
    """Show the current sandbox type and whether Docker is available."""
    from pathlib import Path

    workspace_raw = repl.container.get("runtime.workspace")
    workspace = Path(workspace_raw) if workspace_raw else Path.cwd()

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


def save_setting_to_toml(repl: VeluneREPL, section: str, key: str, value: Any) -> bool:
    """Merge ``[section] key = value`` into velune.toml, atomically. True if written.

    The write goes to a sibling temp file which is flushed, fsynced, and only
    then ``os.replace``-d over the real config. ``os.replace`` is atomic on
    both POSIX and Windows, so a crash or power loss during persistence can
    leave the config either fully old or fully new — never the truncated file
    the previous ``open(path, "w")`` implementation could produce, which would
    have taken every unrelated setting (providers, memory, execution) down
    with it.

    Existing content is read and merged rather than replaced, so writing one
    setting never drops the others. A read failure is *not* treated as "start
    from an empty config": that would silently overwrite a file we simply
    could not parse, so the save is abandoned instead and the last known-good
    configuration is left exactly as it was.
    """
    import os
    import tempfile

    import toml

    try:
        workspace = Path(repl.container.get("runtime.workspace"))
        config_path = repl.container.get("runtime.config_path") or (workspace / "velune.toml")
        config_path = Path(config_path)
    except Exception as exc:
        _log.debug("Could not resolve config path: %s", exc)
        return False

    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            data = toml.load(config_path)
        except Exception as exc:
            _log.warning(
                "velune.toml could not be parsed (%s); refusing to overwrite it. "
                "Setting %s.%s was not saved.",
                exc,
                section,
                key,
            )
            return False

    data.setdefault(section, {})[key] = value

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the target so the replace stays on one filesystem.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=str(config_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, config_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:
        _log.warning("Could not save %s.%s to velune.toml: %s", section, key, exc)
        return False
    return True


def _update_runtime_config(repl: VeluneREPL, section: str, key: str, value: Any) -> None:
    try:
        config = repl.container.get("runtime.config")
        sec_obj = getattr(config, section, None)
        if sec_obj is not None:
            setattr(sec_obj, key, value)
    except Exception:
        pass

    # Custom triggers
    if section == "theme" and key == "active":
        # Apply the theme on screen now, not only on the next launch.
        try:
            from velune.cli import design, themes

            theme, ok = themes.resolve(str(value))
            if ok:
                design.apply_theme(theme)
                ui = getattr(repl, "_fullscreen_ui", None)
                if ui is not None:
                    ui.restyle()
        except Exception as exc:
            _log.debug("Could not apply theme %r live: %s", value, exc)
