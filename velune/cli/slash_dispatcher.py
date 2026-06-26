"""Slash-command registry factory.

Extracted from ``VeluneREPL._build_registry`` so that the command table lives
in one dedicated module rather than adding bulk to the 4,000-line REPL file.

Usage::

    from velune.cli.slash_dispatcher import build_slash_registry
    registry = build_slash_registry(repl_instance)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from velune.cli.slash_commands import SlashCommand, SlashCommandRegistry

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.slash_dispatcher")


# Canonical category for every built-in slash command, co-located with the
# registrations below. Applied onto each command's ``category`` field so /help
# and the completer share one source of truth. A test asserts every registered
# command name appears here (no silent "General" fallback for built-ins).
_BUILTIN_CATEGORIES: dict[str, str] = {
    # Session
    "help": "Session", "exit": "Session", "clear": "Session", "new": "Session",
    "history": "Session", "stats": "Session", "session": "Session", "context": "Session",
    # Workspace
    "project": "Workspace", "index": "Workspace", "diff": "Workspace",
    "undo": "Workspace", "hunk": "Workspace",
    # Models
    "model": "Models", "models": "Models", "pull": "Models", "delete": "Models",
    "bench": "Models", "councilmodel": "Models",
    # Council
    "run": "Council", "council": "Council", "jobs": "Council", "dashboard": "Council",
    # Modes
    "optimus": "Modes", "godly": "Modes", "normal": "Modes", "mode": "Modes",
    # Memory
    "memory": "Memory", "graph": "Memory",
    # Code
    "lint": "Code", "refactor": "Code", "typify": "Code",
    # Git
    "push": "Git", "pr": "Git", "issue": "Git", "sandbox": "Git",
    # Extend
    "mcp": "Extend", "plugin": "Extend",
    # System
    "doctor": "System", "config": "System", "hooks": "System", "approve": "System",
}


def build_slash_registry(repl: VeluneREPL) -> SlashCommandRegistry:
    """Build and return the full SlashCommandRegistry bound to *repl*'s handlers.

    All ``_cmd_*`` methods remain on the REPL instance; this function merely
    registers them into the registry so the REPL class stays focused on
    execution logic rather than command-table book-keeping.
    """
    registry = SlashCommandRegistry()

    # ── Core session ─────────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="help",
            aliases=["h", "?"],
            description="Show all available commands",
            usage="/help",
            handler=repl._cmd_help,
        )
    )
    registry.register(
        SlashCommand(
            name="exit",
            aliases=["quit", "q"],
            description="Exit the Velune session",
            usage="/exit",
            handler=repl._cmd_exit,
        )
    )
    registry.register(
        SlashCommand(
            name="clear",
            aliases=["cls"],
            description="Clear the terminal screen (conversation context is preserved)",
            usage="/clear",
            handler=repl._cmd_clear,
        )
    )
    registry.register(
        SlashCommand(
            name="new",
            aliases=["fresh"],
            description="Start a new conversation session (project memory persists)",
            usage="/new [title]",
            handler=repl._cmd_new,
        )
    )
    registry.register(
        SlashCommand(
            name="project",
            aliases=["proj", "workspace"],
            description="Open, close, or inspect project workspaces (no indexing)",
            usage="/project [open <path>|close|status|list|add <path>|<name|path>]",
            handler=repl._cmd_project,
        )
    )
    registry.register(
        SlashCommand(
            name="index",
            aliases=["cognition", "cog"],
            description="Index the workspace so Velune understands its code: quick, standard, or deep",
            usage="/index [init|quick|standard|deep|status|cancel|rebuild]",
            handler=repl._cmd_cognition,
        )
    )

    # ── Environment / diagnostics ─────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="doctor",
            aliases=["diag"],
            description="Run environment health checks",
            usage="/doctor",
            handler=repl._cmd_doctor,
        )
    )
    registry.register(
        SlashCommand(
            name="config",
            aliases=["cfg"],
            description="Show current system configuration settings",
            usage="/config",
            handler=repl._cmd_config,
        )
    )
    registry.register(
        SlashCommand(
            name="stats",
            aliases=["usage"],
            description="Show session statistics: tokens, cost, turns, uptime",
            usage="/stats",
            handler=repl._cmd_stats,
        )
    )
    registry.register(
        SlashCommand(
            name="history",
            aliases=["hist"],
            description="Show REPL command execution history",
            usage="/history",
            handler=repl._cmd_history,
        )
    )
    registry.register(
        SlashCommand(
            name="hooks",
            aliases=[],
            description="List active lifecycle hooks and their config",
            usage="/hooks",
            handler=repl._cmd_hooks,
        )
    )

    # ── Model management ─────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="model",
            aliases=["m"],
            description="Discover, connect, switch, inspect, or locate models",
            usage=(
                "/model [model-id|discover|connect <id>|use <id>|list|status|"
                "remove <id>|locate|locations]"
            ),
            handler=repl._cmd_model,
        )
    )
    registry.register(
        SlashCommand(
            name="models",
            aliases=["ls"],
            description="List all available models",
            usage="/models",
            handler=repl._cmd_models,
        )
    )
    registry.register(
        SlashCommand(
            name="pull",
            aliases=["download", "get"],
            description="Download an Ollama model interactively",
            usage="/pull [model-id]",
            handler=repl._cmd_pull,
        )
    )
    registry.register(
        SlashCommand(
            name="delete",
            aliases=["remove", "rm"],
            description="Delete a locally installed Ollama model",
            usage="/delete <model-id>",
            handler=repl._cmd_delete,
        )
    )
    registry.register(
        SlashCommand(
            name="councilmodel",
            aliases=["cm", "roles"],
            description="Assign specific models to council agent roles",
            usage="/councilmodel [show|reset]",
            handler=repl._cmd_councilmodel,
        )
    )

    # ── Council / orchestration ───────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="run",
            aliases=["r"],
            description="Execute a task through the Reasoning Council",
            usage="/run <task description>",
            handler=repl._cmd_run,
        )
    )
    registry.register(
        SlashCommand(
            name="council",
            aliases=["c"],
            description="Force full council tier regardless of task complexity",
            usage="/council <task description>",
            handler=repl._cmd_council,
        )
    )
    registry.register(
        SlashCommand(
            name="jobs",
            aliases=["job"],
            description="List background jobs or cancel one (/jobs cancel <id>)",
            usage="/jobs [cancel <id>]",
            handler=repl._cmd_jobs,
        )
    )
    registry.register(
        SlashCommand(
            name="dashboard",
            aliases=["dash"],
            description="Live progress dashboard: jobs, alerts, and provider health",
            usage="/dashboard",
            handler=repl._cmd_dashboard,
        )
    )

    # ── Session / memory ─────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="session",
            aliases=["s"],
            description="Pick, resume, save, or export sessions (no args = interactive picker)",
            usage="/session [list|resume <id>|summary <id>|save|export]",
            handler=repl._cmd_session,
        )
    )
    registry.register(
        SlashCommand(
            name="memory",
            aliases=["mem"],
            description="Inspect memory tiers and stats",
            usage="/memory [clear|stats]",
            handler=repl._cmd_memory,
        )
    )
    registry.register(
        SlashCommand(
            name="context",
            aliases=["ctx"],
            description="Show context window usage for the current conversation",
            usage="/context",
            handler=repl._cmd_context,
        )
    )
    registry.register(
        SlashCommand(
            name="graph",
            aliases=["g"],
            description="Render a hierarchical tree of knowledge graph entities",
            usage="/graph",
            handler=repl._cmd_graph,
        )
    )

    # ── Session mode ─────────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="optimus",
            aliases=["fast", "opt"],
            description="Speed mode — instant tier, compressed context, smallest model",
            usage="/optimus",
            handler=repl._cmd_optimus,
        )
    )
    registry.register(
        SlashCommand(
            name="godly",
            aliases=["full", "god"],
            description="Max power — full council, largest model, full context",
            usage="/godly",
            handler=repl._cmd_godly,
        )
    )
    registry.register(
        SlashCommand(
            name="normal",
            aliases=["reset", "n"],
            description="Return to balanced normal mode",
            usage="/normal",
            handler=repl._cmd_normal,
        )
    )
    registry.register(
        SlashCommand(
            name="mode",
            aliases=[],
            description="Show or switch the session mode: fast | max | normal | status",
            usage="/mode [fast|max|normal|status]",
            handler=repl._cmd_mode,
        )
    )

    # ── Diff / editing ───────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="diff",
            aliases=["d"],
            description="Show uncommitted file changes from the last council run",
            usage="/diff",
            handler=repl._cmd_diff,
        )
    )
    registry.register(
        SlashCommand(
            name="undo",
            aliases=["u"],
            description="Revert the last Velune-generated git commit (keeps changes staged)",
            usage="/undo",
            handler=repl._cmd_undo,
        )
    )
    registry.register(
        SlashCommand(
            name="hunk",
            aliases=["hunks"],
            description="Toggle hunk-by-hunk review mode for edits",
            usage="/hunk",
            handler=repl._cmd_hunk,
        )
    )
    registry.register(
        SlashCommand(
            name="approve",
            aliases=["approval"],
            description="Set tool/command approval mode: safe | ask | block",
            usage="/approve [safe|ask|block]",
            handler=repl._cmd_approve,
        )
    )

    # ── Benchmarks ────────────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="bench",
            aliases=["b"],
            description="View or run empirical model capability benchmarks",
            usage="/bench [run]",
            handler=repl._cmd_bench,
        )
    )

    # ── MCP / plugins ─────────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="mcp",
            aliases=[],
            description="Inspect MCP servers, tools, and resources",
            usage="/mcp [servers|tools|resources|connect <name>|disconnect <name>|refresh <name>]",
            handler=repl._cmd_mcp,
        )
    )
    registry.register(
        SlashCommand(
            name="plugin",
            aliases=["plugins", "pl"],
            description="List, enable, disable, or reload declarative plugins",
            usage="/plugin [list|enable <name>|disable <name>|reload [name]|show <name>]",
            handler=repl._cmd_plugin,
        )
    )

    # ── Git provider ──────────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="push",
            aliases=["gp"],
            description="Push current branch to remote origin",
            usage="/push [--force]",
            handler=repl._cmd_push,
        )
    )
    registry.register(
        SlashCommand(
            name="pr",
            aliases=["pull-request", "mr"],
            description="Create a pull request / merge request on GitHub or GitLab",
            usage="/pr <title> [--base <branch>] [--draft]",
            handler=repl._cmd_pr,
        )
    )
    registry.register(
        SlashCommand(
            name="issue",
            aliases=["gh-issue", "gl-issue"],
            description="Fetch a GitHub/GitLab issue and inject it as conversation context",
            usage="/issue <number>",
            handler=repl._cmd_issue,
        )
    )
    registry.register(
        SlashCommand(
            name="sandbox",
            aliases=["sb"],
            description="Show current sandbox type and status (subprocess or Docker)",
            usage="/sandbox [docker|status]",
            handler=repl._cmd_sandbox,
        )
    )

    # ── Code intelligence ─────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="lint",
            aliases=["check"],
            description="Lint a Python file or the last @mentioned .py files",
            usage="/lint [file]",
            handler=repl._cmd_lint,
        )
    )
    registry.register(
        SlashCommand(
            name="refactor",
            aliases=["smell", "smells"],
            description="Detect code smells in a Python file",
            usage="/refactor <file>",
            handler=repl._cmd_refactor,
        )
    )
    registry.register(
        SlashCommand(
            name="typify",
            aliases=["types", "hints"],
            description="Suggest type hints for unannotated functions in a Python file",
            usage="/typify <file>",
            handler=repl._cmd_typify,
        )
    )

    # Assign the canonical category onto each built-in command so /help and the
    # completer derive grouping from the command itself — never a parallel dict.
    for cmd in registry.all_unique():
        cmd.category = _BUILTIN_CATEGORIES.get(cmd.name, cmd.category)

    # ── TOML-defined user / workspace commands ────────────────────────────────
    # Registered after categorisation so they keep their own category (default
    # "General", surfaced under a trailing group in /help).
    _load_file_commands(repl, registry)

    return registry


def _load_file_commands(repl: VeluneREPL, registry: SlashCommandRegistry) -> None:
    """Discover and register TOML command files from user + workspace dirs."""
    from velune.cli.commands.file_commands import FileCommandLoader

    workspace_path: Path | None = None
    try:
        raw = repl.container.get("runtime.workspace")
        if raw:
            workspace_path = Path(raw)
    except Exception:
        pass

    loader = FileCommandLoader(workspace=workspace_path)
    existing = {cmd.name for cmd in registry.all_unique()}
    try:
        new_cmds = loader.load(repl.console, existing_names=existing)
        for cmd in new_cmds:
            registry.register(cmd)
        if new_cmds:
            _log.debug("Loaded %d TOML command(s)", len(new_cmds))
    except Exception as exc:
        _log.debug("TOML command load error (non-fatal): %s", exc)
