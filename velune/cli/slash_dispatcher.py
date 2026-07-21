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
    # AI
    "run": "AI",
    "council": "AI",
    "jobs": "AI",
    "dashboard": "AI",
    "fast": "AI",
    "max": "AI",
    "normal": "AI",
    "mode": "AI",
    # Providers
    "providers": "Providers",
    "connect": "Providers",
    # Models
    "model": "Models",
    "models": "Models",
    "pull": "Models",
    "delete": "Models",
    "bench": "Models",
    "roles": "Models",
    # Projects
    "project": "Projects",
    "index": "Projects",
    # Memory
    "memory": "Memory",
    "graph": "Memory",
    "context": "Memory",
    # Tools
    "lint": "Tools",
    "refactor": "Tools",
    "types": "Tools",
    "plugin": "Tools",
    "hooks": "Tools",
    # MCP
    "mcp": "MCP",
    # Resources
    "resource": "Resources",
    # Git
    "diff": "Git",
    "undo": "Git",
    "hunk": "Git",
    "push": "Git",
    "pr": "Git",
    "issue": "Git",
    "sandbox": "Git",
    # Settings
    "settings": "Settings",
    "config": "Settings",
    "approve": "Settings",
    # System
    "help": "System",
    "exit": "System",
    "clear": "System",
    "new": "System",
    "history": "System",
    "stats": "System",
    "session": "System",
    "doctor": "System",
    "trace": "System",
    "backup": "System",
    "restore": "System",
    "recover": "System",
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
            description="Show all available commands grouped by category",
            usage="/help [--all]",
            handler=repl._cmd_help,
            examples=("/help", "/help --all"),
            search_terms=("commands", "reference", "documentation", "list"),
            shortcut="/?",
        )
    )
    registry.register(
        SlashCommand(
            name="exit",
            aliases=["quit", "q"],
            description="Exit the Velune session",
            usage="/exit",
            handler=repl._cmd_exit,
            examples=("/exit",),
            search_terms=("quit", "close", "bye", "leave"),
            shortcut="/q",
        )
    )
    registry.register(
        SlashCommand(
            name="clear",
            aliases=["cls"],
            description="Clear the terminal screen (conversation context is preserved)",
            usage="/clear",
            handler=repl._cmd_clear,
            examples=("/clear",),
            search_terms=("screen", "clean terminal", "reset screen"),
            shortcut="/cls",
        )
    )
    registry.register(
        SlashCommand(
            name="new",
            aliases=["fresh"],
            description="Start a new conversation session (project memory persists)",
            usage="/new [title]",
            handler=repl._cmd_new,
            examples=("/new", '/new "feature exploration"'),
            search_terms=("fresh start", "reset conversation", "new chat", "new session"),
            shortcut="/fresh",
        )
    )
    registry.register(
        SlashCommand(
            name="project",
            aliases=["proj", "workspace"],
            description="Open, close, or inspect project workspaces (no indexing)",
            usage="/project [open <path>|close|status|list|add <path>|<name|path>]",
            handler=repl._cmd_project,
            examples=("/project", "/project open ~/myproject", "/project list", "/project close"),
            search_terms=("workspace", "directory", "folder", "open project", "switch project"),
            shortcut="/proj",
        )
    )
    registry.register(
        SlashCommand(
            name="index",
            aliases=["cognition", "cog"],
            description="Index the workspace so Velune understands its code: quick, standard, or deep",
            usage="/index [init|quick|standard|deep|status|cancel|rebuild]",
            handler=repl._cmd_cognition,
            examples=("/index", "/index quick", "/index deep", "/index status"),
            search_terms=(
                "cognition",
                "understand code",
                "code context",
                "codebase",
                "ai context",
                "scan",
            ),
            shortcut="/cog",
        )
    )

    # ── Environment / diagnostics ─────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="providers",
            aliases=["provider", "prov"],
            description="Add, manage, test, and discover models from cloud AI providers",
            usage="/providers [add|manage|test|discover|refresh|remove|status] [provider-id]",
            handler=repl._cmd_providers,
            examples=(
                "/providers",
                "/providers add anthropic",
                "/providers status",
                "/providers discover",
            ),
            search_terms=(
                "anthropic",
                "claude",
                "openai",
                "google",
                "gemini",
                "groq",
                "mistral",
                "deepseek",
                "cohere",
                "nvidia",
                "nvidia nim",
                "xai",
                "grok",
                "meta",
                "llama",
                "huggingface",
                "ollama",
                "api key",
                "connect provider",
                "credentials",
                "auth",
                "add key",
            ),
            shortcut="/prov",
        )
    )
    registry.register(
        SlashCommand(
            name="connect",
            aliases=["login", "auth"],
            category="Providers",
            description="Connect an AI provider — pick one, paste your API key, get it verified",
            usage="/connect [provider-id]",
            handler=repl._cmd_login,
            examples=("/connect", "/connect anthropic"),
            search_terms=(
                "api key",
                "sign in",
                "login",
                "authenticate",
                "connect provider",
                "add key",
                "paste key",
            ),
        )
    )
    registry.register(
        SlashCommand(
            name="doctor",
            aliases=["diag"],
            description="Run environment health checks across all subsystems",
            usage="/doctor",
            handler=repl._cmd_doctor,
            examples=("/doctor",),
            search_terms=("health", "diagnostics", "environment", "check", "broken", "debug setup"),
        )
    )
    registry.register(
        SlashCommand(
            name="trace",
            aliases=["logs"],
            description="Show recent execution trace events for this workspace",
            usage="/trace [limit] [type-filter]",
            handler=repl._cmd_trace,
            examples=("/trace", "/trace 50", "/trace 50 tool_call"),
            search_terms=("trace", "logs", "events", "what happened", "execution log", "debug"),
        )
    )
    registry.register(
        SlashCommand(
            name="backup",
            aliases=[],
            description="Snapshot all Velune state (sessions, config, providers, memory, trust)",
            usage="/backup [path] [--include a,b] [--with-secrets]",
            handler=repl._cmd_backup,
            examples=("/backup", "/backup --include sessions,memory"),
            search_terms=("backup", "snapshot", "archive", "export", "save state"),
        )
    )
    registry.register(
        SlashCommand(
            name="restore",
            aliases=[],
            description="Restore Velune state from a backup archive",
            usage="/restore <archive> [--overwrite] [--dry-run]",
            handler=repl._cmd_restore,
            examples=("/restore velune-backup-20260630.tar.gz", "/restore backup.tar.gz --dry-run"),
            search_terms=("restore", "import", "recover state", "unpack archive"),
        )
    )
    registry.register(
        SlashCommand(
            name="recover",
            aliases=[],
            description="Recover an unsaved session left behind by a crash",
            usage="/recover [id] [--all]",
            handler=repl._cmd_recover,
            examples=("/recover", "/recover --all"),
            search_terms=(
                "recover",
                "crash",
                "unsaved",
                "autosave",
                "lost session",
                "restore session",
            ),
        )
    )
    registry.register(
        SlashCommand(
            name="settings",
            aliases=["setup"],
            description="Interactive settings dashboard (keyboard navigation)",
            usage="/settings",
            handler=repl._cmd_settings,
            examples=("/settings",),
            search_terms=("configure", "preferences", "setup", "dashboard", "options"),
        )
    )
    registry.register(
        SlashCommand(
            name="config",
            aliases=["cfg"],
            description="Show current system configuration settings",
            usage="/config",
            handler=repl._cmd_config,
            examples=("/config",),
            search_terms=("configuration", "show config", "current settings", "velune.toml"),
            shortcut="/cfg",
        )
    )
    registry.register(
        SlashCommand(
            name="stats",
            aliases=["usage"],
            description="Show session statistics: tokens, cost, turns, uptime",
            usage="/stats",
            handler=repl._cmd_stats,
            examples=("/stats",),
            search_terms=("tokens", "cost", "usage", "turns", "uptime", "spending"),
        )
    )
    registry.register(
        SlashCommand(
            name="history",
            aliases=["hist"],
            description="Show REPL command execution history",
            usage="/history",
            handler=repl._cmd_history,
            examples=("/history",),
            search_terms=("previous commands", "command log", "past commands"),
            shortcut="/hist",
        )
    )
    registry.register(
        SlashCommand(
            name="hooks",
            aliases=[],
            description="List active lifecycle hooks and their config",
            usage="/hooks",
            handler=repl._cmd_hooks,
            examples=("/hooks",),
            search_terms=("lifecycle", "events", "callbacks", "pre-run", "post-run"),
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
            examples=(
                "/model",
                "/model discover",
                "/model connect ollama/llama3.2",
                "/model use gpt-4o",
                "/model status",
            ),
            search_terms=(
                "llm",
                "ai model",
                "switch model",
                "ollama",
                "connect model",
                "active model",
            ),
            shortcut="/m",
        )
    )
    registry.register(
        SlashCommand(
            name="models",
            aliases=["ls"],
            description="List all available models with speed, context, and capability info",
            usage="/models",
            handler=repl._cmd_models,
            examples=("/models",),
            search_terms=("list models", "available models", "all models", "model catalogue"),
        )
    )
    registry.register(
        SlashCommand(
            name="pull",
            aliases=["download", "get"],
            description="Download an Ollama model interactively",
            usage="/pull [model-id]",
            handler=repl._cmd_pull,
            examples=("/pull", "/pull llama3.2", "/pull mistral"),
            search_terms=("download model", "install model", "ollama pull", "get model"),
        )
    )
    registry.register(
        SlashCommand(
            name="delete",
            aliases=["remove", "rm"],
            description="Delete a locally installed Ollama model",
            usage="/delete <model-id>",
            handler=repl._cmd_delete,
            examples=("/delete llama3.2",),
            search_terms=("remove model", "uninstall model", "free space"),
        )
    )
    registry.register(
        SlashCommand(
            name="roles",
            aliases=["councilmodel", "cm"],
            description="Assign specific models to each Reasoning Council agent role",
            usage="/roles [show|reset]",
            handler=repl._cmd_councilmodel,
            examples=("/roles", "/roles show", "/roles reset"),
            search_terms=("assign model", "roles", "council roles", "agent roles", "multi-agent"),
            shortcut="/cm",
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
            examples=(
                "/run write a REST endpoint for user auth",
                "/run fix the bug in auth.py",
                "/run explain how the payment flow works",
            ),
            search_terms=("task", "execute", "ai", "ask", "do", "generate", "write code"),
            shortcut="/r",
        )
    )
    registry.register(
        SlashCommand(
            name="council",
            aliases=["c"],
            description="Force full council tier regardless of task complexity",
            usage="/council <task description>",
            handler=repl._cmd_council,
            examples=(
                "/council analyze security vulnerabilities in this codebase",
                "/council review this architecture for scalability issues",
            ),
            search_terms=(
                "full council",
                "multi-agent",
                "complex task",
                "deep analysis",
                "best quality",
            ),
            shortcut="/c",
        )
    )
    registry.register(
        SlashCommand(
            name="jobs",
            aliases=["job"],
            description="List background jobs or cancel one (/jobs cancel <id>)",
            usage="/jobs [cancel <id>]",
            handler=repl._cmd_jobs,
            examples=("/jobs", "/jobs cancel abc123"),
            search_terms=("background", "running", "tasks", "cancel job", "progress", "queue"),
        )
    )
    registry.register(
        SlashCommand(
            name="dashboard",
            aliases=["dash", "status"],
            description="Live system dashboard: session, state, jobs, alerts, health",
            usage="/dashboard",
            handler=repl._cmd_dashboard,
            examples=("/dashboard", "/status"),
            search_terms=(
                "live",
                "monitor",
                "status",
                "health",
                "progress",
                "overview",
                "state",
                "what is velune doing",
                "what's running",
                "current model",
                "active provider",
                "system state",
            ),
            shortcut="/status",
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
            examples=("/session", "/session list", "/session resume", "/session save"),
            search_terms=(
                "history",
                "conversation",
                "resume",
                "save session",
                "past sessions",
                "switch session",
            ),
            shortcut="/s",
        )
    )
    registry.register(
        SlashCommand(
            name="memory",
            aliases=["mem"],
            description="Inspect the 5-tier memory system: working, episodic, semantic, graph, lineage",
            usage="/memory [clear|stats]",
            handler=repl._cmd_memory,
            examples=("/memory", "/memory stats", "/memory clear"),
            search_terms=("remember", "recall", "knowledge", "tiers", "working memory", "episodic"),
            shortcut="/mem",
        )
    )
    registry.register(
        SlashCommand(
            name="context",
            aliases=["ctx"],
            description="Show context window usage for the current conversation",
            usage="/context",
            handler=repl._cmd_context,
            examples=("/context",),
            search_terms=("tokens", "context window", "usage", "conversation length", "how full"),
            shortcut="/ctx",
        )
    )
    registry.register(
        SlashCommand(
            name="graph",
            aliases=["g"],
            description="Render a hierarchical tree of knowledge graph entities",
            usage="/graph",
            handler=repl._cmd_graph,
            examples=("/graph",),
            search_terms=("knowledge graph", "entities", "relationships", "tree", "concepts"),
            shortcut="/g",
        )
    )

    # ── Session mode ─────────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="fast",
            aliases=["optimus", "opt"],
            description="Speed mode — instant tier, compressed context, smallest model",
            usage="/fast",
            handler=repl._cmd_optimus,
            examples=("/fast",),
            search_terms=("fast", "quick", "speed", "instant", "lightweight", "small model"),
        )
    )
    registry.register(
        SlashCommand(
            name="max",
            aliases=["godly", "full", "god"],
            description="Max power — full council, largest model, full context",
            usage="/max",
            handler=repl._cmd_godly,
            examples=("/max",),
            search_terms=("max", "full power", "powerful", "best model", "maximum quality"),
        )
    )
    registry.register(
        SlashCommand(
            name="normal",
            aliases=["reset", "n"],
            description="Return to balanced normal mode",
            usage="/normal",
            handler=repl._cmd_normal,
            examples=("/normal",),
            search_terms=("balanced", "reset mode", "default mode"),
            shortcut="/n",
        )
    )
    registry.register(
        SlashCommand(
            name="mode",
            aliases=[],
            description="Show or switch the session mode: fast | max | normal | status",
            usage="/mode [fast|max|normal|status]",
            handler=repl._cmd_mode,
            examples=("/mode", "/mode fast", "/mode normal", "/mode status"),
            search_terms=("switch mode", "speed mode", "session mode", "change mode"),
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
            examples=("/diff",),
            search_terms=("changes", "uncommitted", "git diff", "edits", "what changed"),
            shortcut="/d",
        )
    )
    registry.register(
        SlashCommand(
            name="undo",
            aliases=["u"],
            description=(
                "Revert the last Velune git commit — not a conversation undo (keeps changes staged)"
            ),
            usage="/undo",
            handler=repl._cmd_undo,
            examples=("/undo",),
            search_terms=("revert", "rollback", "git undo", "undo commit", "take back"),
            shortcut="/u",
        )
    )
    registry.register(
        SlashCommand(
            name="hunk",
            aliases=["hunks"],
            description="Toggle hunk-by-hunk review mode — approve each change before it's applied",
            usage="/hunk",
            handler=repl._cmd_hunk,
            examples=("/hunk",),
            search_terms=("review changes", "approve change", "hunk by hunk", "interactive review"),
        )
    )
    registry.register(
        SlashCommand(
            name="approve",
            aliases=["approval"],
            description="Set tool/command approval mode: safe | ask | block",
            usage="/approve [safe|ask|block]",
            handler=repl._cmd_approve,
            examples=("/approve safe", "/approve ask", "/approve block"),
            search_terms=("permissions", "safety", "tool approval", "confirmation", "auto-approve"),
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
            examples=("/bench", "/bench run"),
            search_terms=("benchmark", "test model", "capability", "performance", "compare models"),
        )
    )

    # ── MCP / plugins ─────────────────────────────────────────────────────────

    registry.register(
        SlashCommand(
            name="mcp",
            aliases=[],
            description="Inspect MCP servers, tools, and resources — connect external capabilities",
            usage="/mcp [servers|tools|resources|connect <name>|disconnect <name>|refresh <name>]",
            handler=repl._cmd_mcp,
            examples=("/mcp", "/mcp servers", "/mcp tools", "/mcp connect myserver"),
            search_terms=(
                "model context protocol",
                "tools",
                "server",
                "resources",
                "external tools",
                "integrations",
            ),
        )
    )
    registry.register(
        SlashCommand(
            name="resource",
            aliases=["resources", "res"],
            description="Connect and inspect local resources — Docker, PostgreSQL, MySQL, Supabase",
            usage="/resource [list|discover|connect <id>|disconnect <id>|status|info <id>]",
            handler=repl._cmd_resource,
            examples=(
                "/resource discover",
                "/resource connect docker",
                "/resource info postgres",
                "/resource status",
            ),
            search_terms=(
                "docker",
                "postgres",
                "postgresql",
                "mysql",
                "mariadb",
                "supabase",
                "database",
                "connectors",
                "integrations",
            ),
        )
    )
    registry.register(
        SlashCommand(
            name="plugin",
            aliases=["plugins", "pl"],
            description="List, enable, disable, or reload declarative TOML/Markdown plugins",
            usage="/plugin [list|enable <name>|disable <name>|reload [name]|show <name>]",
            handler=repl._cmd_plugin,
            examples=("/plugin list", "/plugin enable myplugin", "/plugin reload"),
            search_terms=(
                "extensions",
                "markdown commands",
                "custom commands",
                "SKILL.md",
                "plugins",
            ),
            shortcut="/pl",
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
            examples=("/push", "/push --force"),
            search_terms=("git push", "upload", "remote", "publish branch"),
            shortcut="/gp",
        )
    )
    registry.register(
        SlashCommand(
            name="pr",
            aliases=["pull-request", "mr"],
            description="Create a pull request / merge request on GitHub or GitLab",
            usage="/pr <title> [--base <branch>] [--draft]",
            handler=repl._cmd_pr,
            examples=('/pr "Add user auth" --base main', "/pr my-feature --draft"),
            search_terms=(
                "pull request",
                "merge request",
                "github",
                "gitlab",
                "create pr",
                "open pr",
            ),
        )
    )
    registry.register(
        SlashCommand(
            name="issue",
            aliases=["gh-issue", "gl-issue"],
            description="Fetch a GitHub/GitLab issue and inject it as conversation context",
            usage="/issue <number>",
            handler=repl._cmd_issue,
            examples=("/issue 42", "/issue 123"),
            search_terms=("github issue", "ticket", "bug report", "issue context"),
        )
    )
    registry.register(
        SlashCommand(
            name="sandbox",
            aliases=["sb"],
            description="Show current sandbox type and status (subprocess or Docker)",
            usage="/sandbox [docker|status]",
            handler=repl._cmd_sandbox,
            examples=("/sandbox", "/sandbox docker", "/sandbox status"),
            search_terms=("isolation", "docker", "security", "execution environment", "container"),
            shortcut="/sb",
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
            examples=("/lint", "/lint src/main.py"),
            search_terms=("code quality", "errors", "linting", "syntax", "pyflakes", "warnings"),
        )
    )
    registry.register(
        SlashCommand(
            name="refactor",
            aliases=["smell", "smells"],
            description="Detect code smells in a Python file",
            usage="/refactor <file>",
            handler=repl._cmd_refactor,
            examples=("/refactor src/main.py",),
            search_terms=("code smell", "cleanup", "improve code", "suggestions", "bad patterns"),
        )
    )
    registry.register(
        SlashCommand(
            name="types",
            aliases=["typify", "hints"],
            description="Suggest type hints for unannotated functions in a Python file",
            usage="/types <file>",
            handler=repl._cmd_typify,
            examples=("/types src/main.py",),
            search_terms=("type hints", "annotations", "mypy", "typing", "add types"),
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
