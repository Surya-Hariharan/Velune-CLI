# Architecture

This document describes how Velune is actually built: the process model, the
package layout, and the control flow through the major subsystems. It is
written for engineers working on the codebase, not end users — see the
[README](../README.md) for usage and the [Usage Guide](USAGE_GUIDE.md) for
day-to-day workflows.

Velune is a single Python process (no client/server split by default). It
starts instantly and does no expensive work — indexing, model discovery,
vector store initialization — until a command or REPL turn actually needs it.

---

## 1. Process shape

```
velune (CLI entry)
 └─ Typer app (velune/cli/app.py)
     ├─ one-shot subcommands (velune run, velune doctor, velune mcp, ...)
     └─ REPL (velune chat / bare `velune`)
         └─ VeluneREPL.run() — the interactive loop
```

- **`velune/main.py:main`** is the real `[project.scripts]` entry point. It
  does the absolute minimum before dispatch (`velune --version` is ~40ms)
  and only imports `velune/cli/app.py:create_app()` when a real command runs.
- **`velune/__main__.py`** forwards to the same entry point so
  `python -m velune` behaves identically — this is the documented PATH-free
  fallback on Windows.
- **`velune/cli/main.py`** is a thin backward-compatible shim re-exporting the
  Typer `app` object; new code should not add to it.

### Command registry, not eager imports

Subcommands live under `velune/cli/commands/` (`ask`, `chat`, `run`, `config`,
`doctor`, `mcp`, `memory`, `models`, `providers`, `session`, `workspace`,
`trust`, …) but Velune does **not** import all of them at startup. Each
command is described declaratively in `velune/cli/registry.py`
(`CommandSpec`, `COMMAND_SPECS`) — module path, callable name, and a
`bootstrap` level (`"light"` vs `"full"`). Only when a specific command is
actually invoked does its module get imported, and only `"full"`-bootstrap
commands pay the cost of constructing the Tier-1 subsystems (memory,
retrieval, cognition, orchestration). This is the mechanism behind the
"instant startup, on-demand cognition" design goal.

---

## 2. The REPL loop

`velune/cli/repl.py:VeluneREPL` is the interactive session. `run()`:

1. Builds a `FullscreenREPLUI` (prompt_toolkit-based, `velune/cli/fullscreen.py`).
2. Restores the previously active model/provider.
3. Starts an episodic session (`EpisodicMemory`, see [Memory](#5-memory-system)).
4. Loads and connects configured MCP servers (`MCPServerRegistry`).
5. Discovers and loads declarative plugins (`PluginManager`).
6. Kicks off background auto-indexing (non-blocking) if the workspace is stale.
7. Loops: `ui.read_input()` → dispatch.

Dispatch is a three-way split on the raw input line:

- `/clear` → `_cmd_clear` (screen only; conversation context is preserved)
- text starting with `/` → `_handle_slash_command`
- anything else → `_handle_prompt`

### Slash command dispatch

Slash commands are a real registry, not a big `if/elif` chain:

- `velune/cli/slash_commands.py` defines `SlashCommand` (name, aliases,
  description, usage, handler, category, permissions, …) and
  `SlashCommandRegistry`, a dict-backed registry that warns on alias
  collisions.
- `velune/cli/slash_dispatcher.py:build_slash_registry()` is the single
  source of truth: it registers ~45 built-in commands, each pointing at a
  thin `repl._cmd_*` delegator that lazily imports the real implementation
  from `velune/cli/handlers/*.py` (e.g. `handlers/mcp.py:cmd_mcp`,
  `handlers/council.py:cmd_run`). Handlers are only imported when their
  command actually fires.
- After built-ins, `velune/cli/commands/file_commands.py:FileCommandLoader`
  loads user- and workspace-defined commands from TOML files, so teams can
  add project-specific slash commands without touching Python.
- `VeluneREPL._handle_slash_command` splits `/cmd args`, looks the command
  up, honors a `"confirm"` permission tag (prompts before running
  destructive commands), and calls `cmd.handler(args)`.

See [Slash Commands](SLASH_COMMANDS.md) for the full command reference.

### Tab completion

`velune/cli/autocomplete.py:SlashCompleter` implements fuzzy matching with
tiered scoring — exact match > prefix > substring > subsequence (scored by
match density) — plus a recency boost for recently-used commands. It handles
three completion contexts: `/command` names, `/model <partial-id>` /
`/pull` / `/delete` model-id arguments, and `@@symbol` file/symbol mentions
inline in a prompt.

### Home surface

`velune/cli/home.py` renders the screen shown before the first prompt: a
compact header (active model, workspace, git branch) plus a live
runtime-facts block (indexed file count, cognition DB size, MCP connection
count, provider availability). Facts are cached with a 60-second TTL
(`VeluneREPL._home_state()`) so redraws don't re-probe the filesystem or
network. This replaced the older centered-figlet banner screen, which did
not carry any live state.

### Command palette

`velune/cli/command_palette.py` overlays a `prompt_toolkit` `Float` over the
REPL input when the user types `/` with no trailing space —
`CommandPaletteModel` handles fuzzy search/grouping, `CommandPalette` handles
rendering and key bindings. `Ctrl+F` toggles a favorites view backed by
`FavoritesStore` (`~/.velune/palette_favorites.json`), letting a user pin
frequently-used commands above the fuzzy-matched list.

### Status bar

`velune/cli/statusbar.py` renders the `prompt_toolkit` bottom toolbar:
active model/provider, session mode, a context-usage bar, git branch, MCP
connected/total server count, background job count, and last-turn latency.

---

## 3. Providers

`velune/providers/` holds one adapter per backend (Ollama, LM Studio, Groq,
OpenRouter, OpenAI, Anthropic, xAI, Google, Together, Fireworks, Mistral,
DeepSeek, Cohere, NVIDIA NIM, HuggingFace). Each adapter implements a common
inference/streaming interface; `providers/discovery/` implements per-provider
model catalog discovery (e.g. querying a local Ollama daemon for pulled
models, or a provider's `/models` endpoint). API keys are stored via the OS
keyring (`velune/providers/keystore.py`, AES-GCM-backed), never in plaintext
config files.

---

## 4. Cognition / Council

The council is the multi-agent reasoning pipeline behind `/run` and
`velune run`.

### Orchestrator

`velune/cognition/orchestrator.py:CouncilOrchestrator` is the real
orchestrator. It owns `CouncilArbitrator`, `ArchitectureCognitionAgent`,
`CognitiveFirewall`, `CouncilScheduler` (sequential vs. concurrent execution
across providers), `StyleResolver`, `CouncilAgentFactory`, and
`CognitivePerformanceAnalytics`. It is wired into the DI container as
`runtime.council_orchestrator`; `velune/orchestration/subsystems.py` exposes the
same instance again as `runtime.orchestration_engine` — `velune/orchestration/`
is an alias module, not a separate engine, despite the separate package name.

> **Note for contributors:** `velune/cognition/agents/*.py` and
> `velune/cognition/council_orchestrator.py:BoundedCouncilOrchestrator` are an
> earlier, parallel implementation of the same idea. They have no importers
> in the live path and should be treated as dead code — don't extend them,
> and prefer removing them in a follow-up cleanup rather than maintaining
> two agent implementations side by side.

### Pipeline

`velune/cognition/council_runner.py:CouncilRunner` drives the actual
sequence:

```
Planner → Coder → Reviewer (loop, up to budget.max_review_cycles)
        → Challenger (adversarial pass)
        → DebateSession (scoring, no LLM call)
        → Synthesizer
```

Roles are implemented in `velune/cognition/council/{planner,coder,reviewer,
challenger,synthesizer}.py` as `BaseCouncilAgent` subclasses, built by
`CouncilAgentFactory`. `DebateSession` (`council/debate.py`) is a pure scoring
pass over `ChallengerMessage` objects and the reviewer's `ReviewDecision` —
it does not call a model; `calculate_max_debate_turns` sizes debate depth
from security-severity signals in the proposal.

### Tier classification

`velune/cognition/council/tiers.py:CouncilTier` has **four** tiers —
`INSTANT`, `MINIMAL`, `STANDARD`, `FULL` — not three. `classify_task_tier()`
is a keyword heuristic (e.g. "explain"/"what is" → `INSTANT`;
"refactor"/"security"/"migrate" → `FULL`). `TierClassifier.classify()` wraps
this with a "structural fan-in escalation floor": it scans the prompt for
mentioned source files, looks up their dependents in the repository
knowledge graph, and force-upgrades the tier (≥5 dependents floors at `FULL`)
— it can only escalate a keyword-based decision, never downgrade it. The
three *session modes* in the README (Optimus/Normal/Godly) map onto this
tier system as user-facing overrides, not a separate mechanism.

---

## 5. Memory system

Five tiers, each with its own class and backing store, wired in
`velune/memory/subsystems.py`:

| Tier | Class | Store |
| --- | --- | --- |
| Working | `WorkingMemoryTier` | in-process, TTL-evicted, no persistence |
| Episodic | `EpisodicMemoryTier` / `EpisodicMemory` | shared SQLite |
| Semantic | `SemanticMemoryTier` (Qdrant) / `SemanticMemory` (LanceDB) | two vector stores, see below |
| Graph | `GraphMemoryTier` | shared SQLite |
| Lineage | `LineageMemoryTier` | shared SQLite |

**Two vector stores exist in parallel, deliberately, not by accident:**
`SemanticMemoryTier` is Qdrant-backed and feeds `HybridRetriever` (the
BM25 + vector + graph retrieval used for chat context); `SemanticMemory` is
LanceDB-backed via an `EmbeddingPipeline` and feeds `MemoryLifecycleManager`
and `ThreeBrainCoordinator` (the working/semantic/episodic/graph fusion used
for "fix the auth thing from yesterday"-style recall). Don't collapse these
into "the vector store" when documenting or debugging memory issues — a bug
report about retrieval quality and one about cross-session recall usually
point at different stores.

All SQLite-backed tiers share one `SQLiteConnectionPool`
(`velune/memory/storage/sqlite_pool.py`).

### Persistence paths

State lives **outside the workspace**, under a per-OS app-data root
(`velune/core/paths.py:app_data_root()` — `%LOCALAPPDATA%\Velune` on Windows,
`~/Library/Application Support/Velune` on macOS, `$XDG_DATA_HOME/velune` on
Linux), namespaced per workspace as `workspaces/<name>-<sha1[:10]>/`:

- `velune_cognitive_core.db` — SQLite (episodic/graph/lineage)
- `qdrant_local_store/` — Qdrant (retrieval semantic tier)
- `lancedb_semantic_store/` — LanceDB (lifecycle semantic tier)

A one-time `migrate_legacy_storage()` copies forward (never moves) any
pre-existing in-workspace `.velune/` state from older Velune versions, so a
cloud-synced project folder never silently loses data.

### Retrieval

`velune/retrieval/hybrid.py:HybridRetriever` fuses `BM25Retriever` (lexical),
`VectorRetriever` (Qdrant dense embeddings — pointed at the *same* Qdrant
client as the semantic memory tier to avoid a local-file lock deadlock), and
`GraphRetriever`, then reranks with `CrossEncoderReranker`. The BM25 corpus
is persisted separately at `.velune/retrieval_index.json`.

---

## 6. Repository cognition

`velune/repository/` builds the on-disk index used by tier classification,
retrieval, and code-navigation tools: an AST/symbol index (Tree-sitter when
the `[parsing]` extra is installed), an import/dependency graph, and a
blast-radius estimator. `velune/tools/filesystem/ignore.py` applies
gitignore-syntax exclusion — hardcoded defaults (VCS dirs, caches,
`node_modules/`, build output) plus an optional workspace `.veluneignore` —
consistently across the scanner, indexer, incremental indexer, and
filesystem tools. Indexing is **on-demand** (`/cognition quick|standard|deep`
or `velune project init`), never automatic on launch, though the REPL will
kick off a background refresh if the existing index is stale.

---

## 7. Execution and tools

`velune/tools/subsystems.py` registers the tool surface: filesystem
(`ReadFile`, `WriteFile`, `GrepFiles`, `FindFiles`, …), git (`GitLog`,
`GitDiff`, `GitCommit`, …), `ExecuteCommand`, `TerminalHistory`,
code-navigation (`SemanticCodeSearch`, `SymbolSearch`, `GoToDefinition`,
`FindReferences`), and `WebFetch`.

### Native tool loop

`velune/orchestration/tool_loop.py:ToolLoopRunner` is the agentic loop used
when the active model/provider supports function calling
(`velune/cli/handlers/tool_chat.py:run_tool_chat`, gated on
`execution.native_tools` in config). It calls the model, dispatches
requested tool calls through a permission-gated `authorize_and_execute` path,
feeds results back, and repeats up to `max_tool_turns` (default 10). If
native tool calling isn't available, the REPL falls back to plain streaming
text with the `@@file`-mention-based context injection instead.

### Sandboxing

`velune/execution/executor.py:_build_sandbox` picks between:

- **`SubprocessSandbox`** (default) — enforces an executable allowlist
  (`velune/execution/command_spec.py:ALLOWED_EXECUTABLES`: python, pytest,
  ruff, mypy, git, node, npm, cargo, go, make, cmake, gcc, clang, and a small
  set of read-only shell utilities) and resolves each executable's real path
  via `shutil.which`, rejecting anything outside trusted system/venv path
  prefixes — a PATH-hijack guard.
- **`DockerSandbox`** — opt-in via `execution.docker_sandbox=true`, runs
  commands in a per-session container (default `python:3.12-slim`) with the
  workspace mounted read/write at `/workspace`. Falls back to
  `SubprocessSandbox` with a warning if Docker init fails.

Command approval itself (read vs. write vs. destructive) is classified by
`velune/tools/safety.py:classify_command` and gated by the active
`/approve` mode (`safe` / `ask` / `block`).

---

## 8. MCP (Model Context Protocol)

Velune is both an MCP client and an MCP server. See [MCP.md](MCP.md) for the
full integration guide — transports, trust gating, `.mcp.json` loading, and
the `/mcp` command surface.

---

## 9. Hooks and plugins

- **Hooks** (`velune/hooks/`) — lifecycle events `SessionStart`,
  `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`,
  `MessageDisplay`. Configured via `~/.velune/hooks.json`,
  `<workspace>/.velune/hooks.json`, or a simplified list in
  `velune.toml [hooks]`. A hook is an external command that receives event
  JSON on stdin and may emit a decision/system-message on stdout. `/hooks`
  lists active bindings (read-only).
- **Plugins** (`velune/plugins/`) — declarative, non-code extensions:
  a `plugin.json` manifest (name, version, and paths to a `commands/` dir,
  `skills/` dir, `hooks/hooks.json`, and/or `.mcp.json`). `PluginScanner`
  discovers plugins under `<workspace>/.velune/plugins/`, then
  `~/.velune/plugins/`; `PluginManager` loads them once at REPL startup and
  wires their commands/skills/hooks/MCP servers into the same registries
  used by built-ins. `/plugin list|enable|disable|reload|show` manages them
  at runtime.

---

## 10. Resource connectors

`velune/resources/manager.py:ResourceManager` is a registry + authorization
hub for external data-source connectors — `DockerConnector`,
`PostgresConnector`, `MySQLConnector`, `SupabaseConnector` out of the box.
Every action declares a permission tier (READ / WRITE / EXECUTE / ADMIN);
READ runs unchecked, everything else goes through an `Approver` — the REPL's
default approver shows a Rich confirmation panel and fails closed on any
error. Credentials reuse the same AES-GCM provider keystore as API keys,
namespaced as `resource:<type>:<name>`, and are redacted (password, tokens,
service-role keys) before ever being displayed. `/resource
list|discover|configure|connect|disconnect|info` is the REPL surface.

---

## 11. Observability

`velune/observability/` provides `velune context` (a point-in-time report of
what's indexed and how fresh it is) and `velune trace` (execution event
log), both reading from on-disk state rather than requiring a running
daemon — useful for proving indexing/execution actually happened, not just
that a command returned success.

---

## Package map

```text
velune/
├── cli/              REPL, slash commands, home surface, palette, autocomplete, status bar
│   ├── commands/     Typer subcommands (workspace, session, models, doctor, mcp, resources, ...)
│   ├── handlers/     Slash-command implementations, lazily imported on first use
│   └── slash_dispatcher.py / slash_commands.py / registry.py   Command registries
├── providers/         Per-backend adapters + model discovery + OS-keyring credential storage
├── cognition/         Council: Planner → Coder → Reviewer → Challenger → Synthesizer
│   └── council/       DebateSession, CouncilRunner inputs, per-role agents, tier classifier
├── orchestration/      Alias module exposing CouncilOrchestrator as "orchestration_engine"; native tool loop
├── memory/             5-tier memory (working/episodic/semantic-qdrant+lancedb/graph/lineage)
├── retrieval/          Hybrid retrieval: BM25 + vector (Qdrant) + graph, cross-encoder reranker
├── repository/         AST/symbol indexing, import graph, blast-radius estimator, .veluneignore
├── execution/          Sandboxed command execution (subprocess allowlist + optional Docker), diff/rollback
├── tools/              File-system, git, web-fetch, terminal, code-navigation tool implementations
├── analysis/           Linting, code-smell detection, type-hint inference
├── integrations/        GitHub / GitLab REST clients (push, PR, issues)
├── hooks/              Lifecycle hook config + dispatcher
├── plugins/             Declarative plugin scanner/manager (commands, skills, hooks, MCP servers)
├── resources/           External data-source connectors (Docker/Postgres/MySQL/Supabase), approval-gated
├── mcp/                MCP server + client; stdio/SSE/HTTP/WebSocket transports; trust gating
├── observability/       Context reports, execution trace log
├── hardware/            Hardware detection, tier classification, GPU probe
├── telemetry/           Token tracking, cost estimation, latency profiling
├── models/              Model registry, capability scoring
├── context/             Context window tracking, token counting, compression
├── core/               Loop detector, retry policy, task/job registry, paths, error types, trust
├── kernel/              DI container / service bootstrap
└── daemon/              Background service (server + IPC transport)
```

---

## Known rough edges (for contributors)

- `velune/cognition/agents/*.py` and `BoundedCouncilOrchestrator` are dead
  code from an earlier refactor of the council. They are not wired into the
  DI container and have no live importers — treat `velune/cognition/council/`
  and `CouncilOrchestrator` as the only live implementation.
- `velune/orchestration/` is currently a thin alias over
  `CouncilOrchestrator`, not an independent "ContextOrchestrationEngine" —
  if you're looking for where orchestration logic actually lives, go to
  `velune/cognition/orchestrator.py`.
- `velune/cli/statusbar.py` retains a few fields (`session_cost`,
  `workspace_name`, `retrieval_note`) that are no longer rendered but kept
  for other callers — don't be surprised if they look unused.
