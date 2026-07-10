# Slash Command Reference

This is the reference for commands typed inside the REPL (after you run
`velune`). For terminal commands run *before* the REPL (`velune run`,
`velune doctor`, …), see the [README command table](../README.md#commands) or
run `velune --help`.

Every command below is registered in
[`velune/cli/slash_dispatcher.py`](../velune/cli/slash_dispatcher.py) via
`build_slash_registry()`. Type `/help` inside the REPL for the live,
always-current list (this doc can drift; `/help` cannot). Tab-completion
works for every command and, after `/model `, for model IDs too.

---

## Session

| Command | Does |
| --- | --- |
| `/help` | Show all commands and their aliases |
| `/exit` | Exit Velune |
| `/clear` | Clear the terminal screen (conversation context is preserved) |
| `/new [title]` | Start a new conversation (project memory persists) |

## Workspace & cognition

| Command | Does |
| --- | --- |
| `/project open <path>` | Open a directory as the active workspace |
| `/project close` | Close the current workspace |
| `/project status` | Show the active workspace and its cognition state |
| `/project list` | List recently-opened workspaces (reopen instantly) |
| `/project add <path>` | Remember a workspace without switching to it |
| `/cognition quick` | Fast scan — manifests only, no symbol index |
| `/cognition standard` | Build a full symbol index for the workspace |
| `/cognition deep` | Deep index (symbols + import graph + relationships) |
| `/cognition status` | Show index freshness and progress |
| `/cognition cancel` | Cancel a running cognition job |
| `/cognition rebuild` | Discard and rebuild the index from scratch |

Cognition is never automatic — nothing indexes your repo until you ask, or
until the REPL kicks off a background refresh because an existing index has
gone stale.

## Council / execution

| Command | Does |
| --- | --- |
| `/run <task>` | Execute a task through the Reasoning Council |
| `/run --bg <task>` | Submit task to background — prompt returns immediately |
| `/council <task>` | Force the full council tier regardless of task complexity |
| `/jobs` | List all background jobs (ID, status, phase, elapsed) |
| `/jobs cancel <id>` | Cancel a running background job |
| `/dashboard` | Live progress dashboard (jobs + alerts + provider health) |

`/run` routes through `TierClassifier`, which picks `INSTANT`, `MINIMAL`,
`STANDARD`, or `FULL` council depth based on the task and how many files in
the repo depend on whatever you mention. `/council` skips classification and
always runs the full Planner → Coder → Reviewer → Challenger → Synthesizer
pipeline. See [ARCHITECTURE.md § Cognition](ARCHITECTURE.md#4-cognition--council).

## Models

| Command | Does |
| --- | --- |
| `/model [model-id]` | Switch active model (arrow-key picker if no arg) |
| `/model discover` | Discover locally available models (e.g. Ollama) |
| `/model connect <id>` | Register/connect a model to the active session |
| `/model use <id>` | Set the active model by id |
| `/model status` | Show the active model and connection state |
| `/model remove <id>` | Remove a model from the registry |
| `/models` | List all available models |
| `/pull [model-id]` | Download an Ollama model with live progress |
| `/delete <model-id>` | Delete a locally installed Ollama model |
| `/councilmodel` | Assign specific models to Planner / Coder / Reviewer roles |
| `/bench [run]` | View or run empirical model capability benchmarks |

## Session modes

| Command | Does |
| --- | --- |
| `/optimus` | Speed mode — instant tier, smallest model, 4k context |
| `/godly` | Max power — full council, largest model, 128k context |
| `/normal` | Return to balanced mode (auto-tier, 16k context) |
| `/mode` | Show current mode settings and active council tier |

These are user-facing presets over the same `CouncilTier` system `/run`
uses automatically — they don't add new machinery, they pin the tier and
context budget so you don't have to think about it mid-task.

## Memory & context

| Command | Does |
| --- | --- |
| `/memory [clear\|stats]` | Inspect or clear memory tiers |
| `/session` | Interactive session picker (list / resume / save / export) |
| `/context` | Show context window usage for the current conversation |
| `/graph` | Render a tree of knowledge graph entities |

`/memory clear` prompts for confirmation — it drops persisted state (SQLite,
Qdrant, LanceDB) for the current workspace, not just the in-memory turn
buffer.

## Diffs & editing

| Command | Does |
| --- | --- |
| `/diff` | Show uncommitted file changes from the last council run |
| `/undo` | Revert the last Velune-generated git commit (keeps changes staged) |
| `/hunk` | Toggle hunk-by-hunk review mode for edits |
| `/approve [safe\|ask\|block]` | Set tool/command approval gate |

`/approve` controls how much Velune can do without asking: `safe` auto-runs
read-only actions and prompts for writes/exec, `ask` prompts for everything
non-trivial, `block` requires explicit confirmation for every tool call.

## Git integration

| Command | Does |
| --- | --- |
| `/push [--force]` | Push current branch to remote origin |
| `/pr <title>` | Create a pull request / merge request on GitHub or GitLab |
| `/issue <number>` | Fetch a GitHub/GitLab issue and inject it as context |
| `/sandbox [docker\|status]` | Show or switch sandbox type (subprocess or Docker) |

## Code intelligence

| Command | Does |
| --- | --- |
| `/lint [file]` | Lint a Python file and display diagnostic output |
| `/refactor <file>` | Detect code smells and suggest refactoring targets |
| `/typify <file>` | Suggest type hints for unannotated functions |

## MCP & plugins

| Command | Does |
| --- | --- |
| `/mcp [servers\|tools\|resources\|connect\|disconnect\|refresh]` | Inspect and manage MCP servers/tools |
| `/plugin [list\|enable\|disable\|reload\|show]` | Manage declarative plugins |

`/mcp` has no `add` subcommand — new servers are declared in `.mcp.json` (or
`velune.toml [mcp.servers]`) and picked up by `/mcp connect` or the
30-second file-watch hot-reload. See [MCP.md](MCP.md).

## Resources

| Command | Does |
| --- | --- |
| `/resource list` / `/resource status` | List configured connectors and their state |
| `/resource discover` | Detect available local resources (e.g. a running Docker daemon) |
| `/resource configure <id>` | Interactively configure and store encrypted credentials |
| `/resource connect <id>` | Connect a configured resource |
| `/resource disconnect <id>` | Disconnect a resource |
| `/resource info <id>` | Show a connector's capability table and permission tiers |

Every action beyond READ (write/execute/admin) goes through an approval
prompt — see [ARCHITECTURE.md § Resource connectors](ARCHITECTURE.md#10-resource-connectors).

## Diagnostics

| Command | Does |
| --- | --- |
| `/doctor` | Run environment health checks |
| `/config` | Show current configuration settings |
| `/stats` | Session statistics: tokens, cost, turns, uptime |
| `/history` | Show REPL command execution history |
| `/hooks` | List active lifecycle hooks and their configuration |

---

## Adding your own commands

Two extension points, no core code changes required:

1. **File-based commands** — drop a TOML file describing a command into
   your workspace or user config; `FileCommandLoader` picks it up at
   startup alongside the built-ins.
2. **Declarative plugins** — a `plugin.json` manifest plus a `commands/`
   directory ships one or more slash commands (and optionally skills,
   hooks, and MCP servers) as a unit. See
   [ARCHITECTURE.md § Hooks and plugins](ARCHITECTURE.md#9-hooks-and-plugins).

Both are loaded into the same `SlashCommandRegistry` used by built-ins, so
they get tab-completion, palette search, and `/help` listing for free.
