# Slash Commands

*The full reference for everything you can type inside the Velune CLI REPL —
43 commands across 12 categories.*

For commands run in the terminal *before* the REPL starts (`velune run`,
`velune doctor`, …), see the [command table in the reference](reference.md#commands)
or run `velune --help`.

---

## Contents

- [AI](#ai)
- [Execution](#execution)
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
| `council` | `c` | `/council [--bg] [--skill <name>] [--permission <name>] <task>  ·  /council show [run]  ·  /council models [set <role> <provider/model> \| clear <role\|--all>]` | A team of agents, picked for the task, works on it in stages |
| `skills` | *(none)* | `/skills [show <name>]` | List the council's skills and permission profiles, or show one |
| `jobs` | `job` | `/jobs [task\|cognition\|shell] [cancel <id>]` | List background jobs (optionally filtered by kind) or cancel one |
| `dashboard` | `dash`, `status` | `/dashboard` | Live system dashboard: session, state, jobs, alerts, health |
| `mode` | *(none)* | `/mode [fast\|max\|normal\|status]` | Show or switch the session mode: fast \| max \| normal \| status |
| `retry` | *(none)* | `/retry [model]` | Regenerate the last response, optionally on a different model |

> `/council` picks a skill — coding, planning, review or
> research — and a permission profile for the task, creates a team of up to
> five agents, shows the plan, and runs it in stages. `--skill` and
> `--permission` choose them yourself; `/skills` lists both. One `/rewind`
> undoes everything a run changed, and `/council show` reopens a past run.
> Every agent uses the chat model unless its role has one of its own:
> `/council models set reviewer <provider/model>` (or `lead`, for whichever
> agent leads) saves it under `[swarm.models]` in `velune.toml`.
> `/council --bg <task>` runs the task as a read-only background job; follow
> it with `/jobs`.
> `/mode fast`, `/mode max` and `/mode normal` set the model, context budget
> and retrieval depth for the session.

---

## Execution

Who may change what. The execution mode decides whether Velune asks before
changing your workspace; it is separate from the fast / normal / max modes
above, which choose how hard the model thinks. Press **Shift+Tab** to cycle
MANUAL → PLAN → AUTO; the status bar shows the current mode (● MANUAL, ⏸ PLAN,
⏵⏵ AUTO) and briefly confirms each switch. While a plan is pending the badge says so,
e.g. `⏸ PLAN · awaiting approval`.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `manual` | *(none)* | `/manual` | Manual mode: read freely, ask before every change |
| `plan` | *(none)* | `/plan [task]` | Plan mode: write a plan to .velune/plans/, change nothing until you approve |
| `auto` | *(none)* | `/auto` | Auto mode: change the workspace without asking (high-risk still asks) |

> **MANUAL** (default) — Velune reads, searches and analyzes freely, and
> shows you every change (files, folders, state-changing commands) for
> approval first. Related changes are previewed together.
>
> **PLAN** — Velune investigates and writes a plan to
> `.velune/plans/<name>.md` (objective, findings, proposed changes, files
> affected, risks, rollback, execution order) and stops with *Waiting for
> approval.* Ask for changes as often as you like — the plan is revised,
> nothing runs. Reply with an explicit approval (*approve*, *go ahead*,
> *implement it*) to execute it; anything outside the plan's file list asks
> again. Approval only counts in PLAN mode, and only for the plan it was given
> to: leaving PLAN mode pauses a running plan (it needs a fresh approval to
> continue), and a plan that is still waiting stays unapproved however you
> switch modes.
>
> **AUTO** — Velune creates, edits and deletes files and runs commands inside
> the workspace without asking, iterating until the task is done. It never
> changes anything outside the workspace — switch to MANUAL to allow a
> location. Locations you allowed earlier in MANUAL do not carry into AUTO.
>
> **Still confirmed in every mode:** high-risk operations (`git reset --hard`,
> `git clean`, force-push, recursive deletes, system or global installs,
> environment/registry/firewall changes) and reading or changing secrets
> (`.env`, keys, `~/.ssh`). In MANUAL and PLAN, anything outside the
> workspace also asks (allow once / for this task / deny); in AUTO reading
> outside it asks and changing it is refused. A command that names a path
> outside the workspace (`../x`, an absolute path) counts as outside.
> Commands such as `sudo` or disk formatting are never run. These rules are
> enforced in code at the tool layer, not by instructions to the model. Start
> in a mode with `velune --mode manual|plan|auto` or set `[execution] mode`
> in `velune.toml`.

---

## Providers

Connecting and managing cloud/local AI providers.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `connect` | `login`, `auth` | `/connect [provider-id]` | Connect an AI provider — pick one, paste your API key, get it verified |

> `/connect` is the only provider command in the REPL. Keys are AES-GCM
> encrypted at rest, with the master key held in your OS keyring. `/login`
> still works as an alias, but `/connect` is the name that actually describes
> what it does — this isn't a username/password sign-in.
>
> **`/providers` (and `/provider`, `/prov`) was removed.** It duplicated
> `/connect` behind a management menu, so two commands both meant "connect a
> provider". Its management-only operations were *not* dropped — they live in
> the terminal CLI, outside the REPL:
>
> | Was | Now |
> | --- | --- |
> | `/providers add` | `/connect` |
> | `/providers status` | `velune provider status` |
> | `/providers test` | `velune provider test [id]` |
> | `/providers manage` | `velune provider inspect <id>` / `edit <id>` |
> | `/providers remove` | `velune provider remove <id>` |
> | `/providers discover` | `/model discover` |

---

## Models

Discovering, connecting and managing models. Benchmarks run from the terminal:
`velune models benchmark`.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `model` | `m` | `/model [model-id\|discover\|connect <id>\|use <id>\|list\|status\|remove <id>\|locate\|locations\|register <path>\|recheck [id]\|relocate <id> <path>]` | Discover, connect, switch, inspect, or locate models |
| `pull` | `download`, `get` | `/pull [model-id]` | Download an Ollama model interactively |
| `delete` | `remove`, `rm` | `/delete <model-id>` | Delete a locally installed Ollama model |

---

## Projects

Opening workspaces and indexing them so Velune CLI understands the code.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `project` | `proj`, `workspace` | `/project [open <path>\|close\|status\|list\|add <path>\|<name\|path>]` | Open, close, or inspect project workspaces (no indexing) |
| `index` | `cognition`, `cog` | `/index [init\|quick\|standard\|deep\|status\|cancel\|rebuild]` | Index the workspace so Velune understands its code: quick, standard, or deep |

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

Inspecting the 5-tier memory system and the context window.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `memory` | `mem` | `/memory [clear\|stats]` | Inspect the 5-tier memory system: working, episodic, semantic, graph, lineage |
| `context` | `ctx` | `/context` | Show context window usage for the current conversation |

> `/memory clear` asks for confirmation before it runs — it drops
> persisted state (SQLite, Qdrant, LanceDB) for the current workspace, not
> just the in-process turn buffer.

---

## Tools

Lifecycle hooks and the plugin system.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `hooks` | *(none)* | `/hooks` | List active lifecycle hooks and their config |
| `plugin` | `plugins`, `pl` | `/plugin [list\|enable <name>\|disable <name>\|reload [name]\|show <name>]` | List, enable, disable, or reload declarative TOML/Markdown plugins |

---

## MCP

Inspecting and connecting Model Context Protocol servers.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `mcp` | *(none)* | `/mcp [servers\|tools\|resources\|connect <name>\|disconnect <name>\|refresh <name>]` | Inspect MCP servers, tools, and resources — connect external capabilities |

> `/mcp` has no `add` subcommand — new servers are declared in `.mcp.json`
> (or `velune.toml [mcp.servers]`) and picked up by `/mcp connect` or the
> periodic file-watch hot-reload. See [mcp.md](mcp.md).

---

## Resources

Connecting to local infrastructure — Docker, databases — as approval-gated
resources.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `resource` | `resources`, `res` | `/resource [list\|discover\|configure <id>\|connect <id>\|disconnect <id>\|status\|info <id>]` | Connect and inspect local resources — Docker, PostgreSQL, MySQL, Supabase |

> `/resource configure <id>` interactively collects and encrypts
> credentials for a database connector (aliases: `set`, `config`). Resource
> actions are decided by the same permission gate as the agent's own tools, so
> they follow your execution mode: reads always run, Manual asks before any
> change, Plan refuses changes, and Auto runs ordinary changes but still asks
> for destructive ones and for anything this task did not create (for example a
> container that was already running). `/resource info <id>` lists each action
> with its permission scope, such as `docker.container.stop`.

---

## Git

Git and forge (GitHub/GitLab) integration, plus the execution sandbox.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `diff` | `d` | `/diff` | Show the uncommitted changes in the working tree |
| `rewind` | *(none)* | `/rewind` | Undo the file changes the agent made in its last turn (skips files changed since) |
| `push` | `gp` | `/push [--force]` | Push current branch to remote origin |
| `pr` | `pull-request`, `mr` | `/pr <title> [--base <branch>] [--draft]` | Create a pull request / merge request on GitHub or GitLab |
| `issue` | `gh-issue`, `gl-issue` | `/issue <number>` | Fetch a GitHub/GitLab issue and inject it as conversation context |
| `sandbox` | `sb` | `/sandbox [docker\|status]` | Show current sandbox type and status (subprocess or Docker) |

---

## Settings

Configuration and appearance.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `config` | `cfg` | `/config` | Show current system configuration settings |
| `theme` | `appearance` | `/theme [theme-id\|colorblind\|motion] [on\|off]` | Switch the colour theme, or toggle an accessibility modifier |
| `crashreports` | `crash-reports` | `/crashreports [on\|off]` | Show or toggle opt-in local crash reporting (off by default, never transmitted) |

> Typing `/theme` and pressing Enter opens the **theme palette** — arrow keys
> to browse, Enter to apply. The change is immediate and applies to every
> surface (transcript, prompt, palettes, status bar, panels); nothing needs
> restarting. The choice is saved to `[theme] active` in `velune.toml`.
>
> Themes: `velune` (default), `amber`, `emerald`, `crimson`, `azure`.
> `/theme emerald` applies one directly without opening the palette.
>
> **Themes and accessibility settings are separate.** A theme is a colour
> palette; `colorblind` (Okabe-Ito severity colours) and `motion` (static
> spinners) are independent modifiers that layer on top and survive a theme
> change. `/theme colorblind on` and `/theme motion off` still work exactly
> as before.
>

---

## System

Session lifecycle, diagnostics, and disaster recovery.

| Command | Aliases | Usage | Description |
| --- | --- | --- | --- |
| `help` | `h`, `?` | `/help [--all] [query]` | Show all available commands, or search them by keyword |
| `exit` | `quit`, `q` | `/exit` | Exit the Velune session |
| `clear` | `cls` | `/clear` | Clear the terminal screen (conversation context is preserved) |
| `new` | `fresh` | `/new [title]` | Start a new conversation session (project memory persists) |
| `fork` | `f` | `/fork [turn]` | Branch into a new session seeded from an earlier turn (original is preserved) |
| `doctor` | `diag` | `/doctor` | Run environment health checks across all subsystems |
| `trace` | `logs` | `/trace [limit] [type-filter]` | Show recent execution trace events for this workspace |
| `backup` | *(none)* | `/backup [path] [--include a,b] [--with-secrets]` | Snapshot all Velune state (sessions, config, providers, memory, trust) |
| `restore` | *(none)* | `/restore <archive> [--include a,b] [--overwrite] [--dry-run]` | Restore Velune state from a backup archive |
| `recover` | *(none)* | `/recover [id] [--all]` | Recover an unsaved session left behind by a crash |
| `stats` | `usage` | `/stats` | Show session statistics: tokens, cost, turns, uptime |
| `history` | `hist` | `/history` | Show REPL command execution history |
| `session` | `s` | `/session [list\|resume <id>\|summary <id>\|save\|export\|import <path>\|rename <id> <title>\|search <query>]` | Pick, resume, save, export, import, rename, or search sessions (no args = interactive picker) |

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
| `appearance` | `/theme` | `login` | `/connect` |
| `auth` | `/connect` | `logs` | `/trace` |
| `c` | `/council` | `m` | `/model` |
| `cfg` | `/config` | `mem` | `/memory` |
| `cls` | `/clear` | `mr` | `/pr` |
| `cog` | `/index` | `pl` | `/plugin` |
| `cognition` | `/index` | `plugins` | `/plugin` |
| `crash-reports` | `/crashreports` | `proj` | `/project` |
| `ctx` | `/context` | `pull-request` | `/pr` |
| `d` | `/diff` | `q` | `/exit` |
| `dash` | `/dashboard` | `quit` | `/exit` |
| `diag` | `/doctor` | `remove` | `/delete` |
| `download` | `/pull` | `res` | `/resource` |
| `f` | `/fork` | `resources` | `/resource` |
| `fresh` | `/new` | `rm` | `/delete` |
| `get` | `/pull` | `s` | `/session` |
| `gh-issue` | `/issue` | `sb` | `/sandbox` |
| `gl-issue` | `/issue` | `status` | `/dashboard` |
| `gp` | `/push` | `usage` | `/stats` |
| `h` | `/help` | `workspace` | `/project` |
| `hist` | `/history`  | |

</details>

---

## Adding your own commands

Two extension points, no core code changes required:

1. **File-based commands** — drop a TOML file describing a command into
   your workspace or user config; `FileCommandLoader` picks it up at
   startup alongside the built-ins.
2. **Declarative plugins** — a `plugin.json` manifest plus a `commands/`
   directory ships one or more slash commands (and optionally skills,
   hooks, and MCP servers) as a unit. A plugin is a directory with a `.velune-plugin/plugin.json`
   manifest; Velune looks for plugins in `<workspace>/.velune/plugins/` and `~/.velune/plugins/`, and
   `/plugin list|enable|disable|reload|show` manages them. See [development.md](development.md) for the
   extension points.

Both are loaded into the same `SlashCommandRegistry` used by built-ins, so
they get tab-completion, palette search, and `/help` listing for free.

---

Back to [reference.md § Commands](reference.md#commands).
