# Slash Commands

*The full reference for everything you can type inside the Velune CLI REPL —
49 commands across 11 categories.*

For commands run in the terminal *before* the REPL starts (`velune run`,
`velune doctor`, …), see the [README command table](../README.md#commands)
or run `velune --help`.

---

## Contents

- [AI](#ai)
- [Providers](#providers)
- [Models](#models)
- [Projects](#projects)
- [Memory](#memory)
- [Tools](#tools)
- [MCP](#mcp)
- [Resources](#resources)
- [Git](#git)
- [Settings](#settings)
- [System](#system)
- [Alias quick lookup](#alias-quick-lookup)
- [Adding your own commands](#adding-your-own-commands)

---

Every command below is registered in
[`velune/cli/slash_dispatcher.py`](../velune/cli/slash_dispatcher.py) via
`build_slash_registry()`, and grouped into categories by the canonical
`_BUILTIN_CATEGORIES` table in that same file. Type `/help` inside the REPL
for the live, always-current list — this doc can drift; `/help` cannot.
Tab-completion works for every command and, after `/model `, for model IDs
too.

> Commands loaded from workspace TOML files or declarative plugins register
> into this same table at startup, so they get tab-completion, palette
> search, and `/help` listing for free — see
> [Adding your own commands](#adding-your-own-commands).

---

## AI

Running tasks through the Reasoning Council, and controlling how much of
it runs.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `run` | `r` | `/run <task description>` | Execute a task through the Reasoning Council |
| `council` | `c` | `/council <task description>` | Force full council tier regardless of task complexity |
| `jobs` | `job` | `/jobs [cancel <id>]` | List background jobs or cancel one |
| `dashboard` | `dash`, `status` | `/dashboard` | Live system dashboard: session, state, jobs, alerts, health |
| `optimus` | `fast`, `opt` | `/optimus` | Speed mode — instant tier, compressed context, smallest model |
| `godly` | `full`, `god` | `/godly` | Max power — full council, largest model, full context |
| `normal` | `reset`, `n` | `/normal` | Return to balanced normal mode |
| `mode` | *(none)* | `/mode [fast|max|normal|status]` | Show or switch the session mode |

> `/run` routes through `TierClassifier`, which picks `INSTANT`, `MINIMAL`,
> `STANDARD`, or `FULL` council depth based on the task and how many files
> in the repo depend on whatever you mention. `/council` skips
> classification and always runs the full
> Planner → Coder → Reviewer → Challenger → Synthesizer pipeline. `/optimus`,
> `/godly`, and `/normal` are user-facing presets over that same
> `CouncilTier` system — they pin the tier and context budget so you don't
> have to think about it mid-task.

---

## Providers

Connecting and managing cloud/local AI providers.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `providers` | `provider`, `prov` | `/providers [add\|manage\|test\|discover\|refresh\|remove\|status] [provider-id]` | Add, manage, test, and discover models from cloud AI providers |
| `login` | `auth` | `/login [provider-id]` | Connect an AI provider — pick one, paste your API key, get it verified |

> Keys are stored in your OS keyring, encrypted at rest. `/login` is the
> fast path for a single provider; `/providers` is the full management
> surface (test, discover models, refresh catalog, remove).

---

## Models

Discovering, connecting, and benchmarking models — including per-role
council assignment.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `model` | `m` | `/model [model-id\|discover\|connect <id>\|use <id>\|list\|status\|remove <id>\|locate\|locations]` | Discover, connect, switch, inspect, or locate models |
| `models` | `ls` | `/models` | List all available models with speed, context, and capability info |
| `pull` | `download`, `get` | `/pull [model-id]` | Download an Ollama model interactively |
| `delete` | `remove`, `rm` | `/delete <model-id>` | Delete a locally installed Ollama model |
| `bench` | `b` | `/bench [run]` | View or run empirical model capability benchmarks |
| `councilmodel` | `cm`, `roles` | `/councilmodel [show\|reset]` | Assign specific models to each Reasoning Council agent role |

---

## Projects

Opening workspaces and indexing them so Velune CLI understands the code.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `project` | `proj`, `workspace` | `/project [open <path>\|close\|status\|list\|add <path>\|<name\|path>]` | Open, close, or inspect project workspaces (no indexing) |
| `index` | `cognition`, `cog` | `/index [init\|quick\|standard\|deep\|status\|cancel\|rebuild]` | Index the workspace so Velune CLI understands its code: quick, standard, or deep |

> `/index` is never automatic — nothing indexes your repo until you ask,
> or until the REPL kicks off a background refresh because an existing
> index has gone stale. Bare `/index` behaves like `/index init` (a
> standard-depth scan with an intro banner). `/index rebuild` runs the
> same full reindex as `/index deep` — there's no separate "wipe and
> start clean" mode, since a deep pass already rebuilds from scratch.
> Every run previews the file count first and asks you to confirm before
> indexing. `/cognition` is kept as a full alias of `/index` for anyone
> used to the old name.

---

## Memory

Inspecting the 5-tier memory system and the knowledge graph built from it.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `memory` | `mem` | `/memory [clear\|stats]` | Inspect the 5-tier memory system: working, episodic, semantic, graph, lineage |
| `graph` | `g` | `/graph` | Render a hierarchical tree of knowledge graph entities |
| `context` | `ctx` | `/context` | Show context window usage for the current conversation |

> `/memory clear` asks for confirmation before it runs — it drops
> persisted state (SQLite, Qdrant, LanceDB) for the current workspace, not
> just the in-process turn buffer.

---

## Tools

Code intelligence and the plugin system.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `lint` | `check` | `/lint [file]` | Lint a Python file or the last @mentioned .py files |
| `refactor` | `smell`, `smells` | `/refactor <file>` | Detect code smells in a Python file |
| `typify` | `types`, `hints` | `/typify <file>` | Suggest type hints for unannotated functions in a Python file |
| `plugin` | `plugins`, `pl` | `/plugin [list\|enable <name>\|disable <name>\|reload [name]\|show <name>]` | List, enable, disable, or reload declarative TOML/Markdown plugins |
| `hooks` | *(none)* | `/hooks` | List active lifecycle hooks and their config |

---

## MCP

Inspecting and connecting Model Context Protocol servers.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `mcp` | *(none)* | `/mcp [servers\|tools\|resources\|connect <name>\|disconnect <name>\|refresh <name>]` | Inspect MCP servers, tools, and resources |

> `/mcp` has no `add` subcommand — new servers are declared in `.mcp.json`
> (or `velune.toml [mcp.servers]`) and picked up by `/mcp connect` or the
> periodic file-watch hot-reload. See [MCP.md](MCP.md).

---

## Resources

Connecting to local infrastructure — Docker, databases — as approval-gated
resources.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `resource` | `resources`, `res` | `/resource [list\|discover\|configure <id>\|connect <id>\|disconnect <id>\|status\|info <id>]` | Connect and inspect local resources — Docker, PostgreSQL, MySQL, Supabase |

> `/resource configure <id>` interactively collects and encrypts
> credentials for a database connector (aliases: `set`, `config`). Every
> action beyond read access — write, execute, admin — goes through an
> approval prompt.

---

## Git

Git and forge (GitHub/GitLab) integration, plus the execution sandbox.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `diff` | `d` | `/diff` | Show uncommitted file changes from the last council run |
| `undo` | `u` | `/undo` | Revert the last Velune CLI-generated git commit (keeps changes staged) |
| `hunk` | `hunks` | `/hunk` | Toggle hunk-by-hunk review mode — approve each change before it's applied |
| `push` | `gp` | `/push [--force]` | Push current branch to remote origin |
| `pr` | `pull-request`, `mr` | `/pr <title> [--base <branch>] [--draft]` | Create a pull request / merge request on GitHub or GitLab |
| `issue` | `gh-issue`, `gl-issue` | `/issue <number>` | Fetch a GitHub/GitLab issue and inject it as conversation context |
| `sandbox` | `sb` | `/sandbox [docker\|status]` | Show current sandbox type and status |

---

## Settings

Configuration and tool/command approval gating.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `settings` | `setup` | `/settings` | Interactive settings dashboard (keyboard navigation) |
| `config` | `cfg` | `/config` | Show current system configuration settings |
| `approve` | `approval` | `/approve [safe\|ask\|block]` | Set tool/command approval mode |

> `/approve` controls how much Velune CLI can do without asking: `safe`
> auto-runs read-only actions and prompts for writes/exec, `ask` prompts
> for everything non-trivial, `block` requires explicit confirmation for
> every tool call.

---

## System

Session lifecycle, diagnostics, and disaster recovery.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `help` | `h`, `?` | `/help [--all]` | Show all available commands grouped by category |
| `exit` | `quit`, `q` | `/exit` | Exit the Velune CLI session |
| `clear` | `cls` | `/clear` | Clear the terminal screen (conversation context is preserved) |
| `new` | `fresh` | `/new [title]` | Start a new conversation session (project memory persists) |
| `history` | `hist` | `/history` | Show REPL command execution history |
| `stats` | `usage` | `/stats` | Show session statistics: tokens, cost, turns, uptime |
| `session` | `s` | `/session [list\|resume <id>\|summary <id>\|save\|export]` | Pick, resume, save, or export sessions (no args = interactive picker) |
| `doctor` | `diag` | `/doctor` | Run environment health checks across all subsystems |
| `backup` | *(none)* | `/backup [path] [--include a,b] [--with-secrets]` | Snapshot all Velune CLI state (sessions, config, providers, memory, trust) |
| `restore` | *(none)* | `/restore <archive> [--include a,b] [--overwrite] [--dry-run]` | Restore Velune CLI state from a backup archive |
| `recover` | *(none)* | `/recover [id] [--all]` | Recover an unsaved session left behind by a crash |

> `/backup` never writes API keys to the archive unless you pass
> `--with-secrets`, which prompts for a passphrase and encrypts them with
> AES-GCM. `/restore` supports the same `--include a,b` subsystem filter
> as `/backup` (valid names: sessions, config, providers, memory, trust).

---

## Alias quick lookup

<details>
<summary><strong>Every alias, alphabetically</strong> — click to expand</summary>

| Alias | Command | Alias | Command |
| --- | --- | --- | --- |
| `?` | `/help` | `job` | `/jobs` |
| `approval` | `/approve` | `ls` | `/models` |
| `auth` | `/login` | `m` | `/model` |
| `b` | `/bench` | `mem` | `/memory` |
| `c` | `/council` | `mr` | `/pr` |
| `cfg` | `/config` | `n` | `/normal` |
| `check` | `/lint` | `opt` | `/optimus` |
| `cls` | `/clear` | `pl` | `/plugin` |
| `cm` | `/councilmodel` | `plugins` | `/plugin` |
| `cog` | `/index` | `proj` | `/project` |
| `cognition` | `/index` | `prov` | `/providers` |
| `ctx` | `/context` | `provider` | `/providers` |
| `d` | `/diff` | `pull-request` | `/pr` |
| `dash` | `/dashboard` | `q` | `/exit` |
| `diag` | `/doctor` | `quit` | `/exit` |
| `download` | `/pull` | `r` | `/run` |
| `fast` | `/optimus` | `remove` | `/delete` |
| `fresh` | `/new` | `reset` | `/normal` |
| `full` | `/godly` | `res` | `/resource` |
| `g` | `/graph` | `resources` | `/resource` |
| `get` | `/pull` | `rm` | `/delete` |
| `gh-issue` | `/issue` | `roles` | `/councilmodel` |
| `gl-issue` | `/issue` | `s` | `/session` |
| `god` | `/godly` | `sb` | `/sandbox` |
| `gp` | `/push` | `setup` | `/settings` |
| `h` | `/help` | `smell` | `/refactor` |
| `hints` | `/typify` | `smells` | `/refactor` |
| `hist` | `/history` | `status` | `/dashboard` |
| `hunks` | `/hunk` | `types` | `/typify` |
| | | `u` | `/undo` |
| | | `usage` | `/stats` |
| | | `workspace` | `/project` |

</details>

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

---

Back to [README.md § Commands](../README.md#commands).
