# Velune

> Local-first multi-model AI developer CLI. Council-based agents,
> persistent memory, repository cognition.
> No cloud required. No quota. No lock-in.

[![PyPI](https://img.shields.io/pypi/v/velune-cli)](https://pypi.org/project/velune-cli/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/Surya-Hariharan/Velune-CLI/ci.yml?branch=main&label=CI)](https://github.com/Surya-Hariharan/Velune-CLI/actions/workflows/ci.yml)

---

## What it does

Velune is a terminal-first AI coding assistant that runs a council of
specialized agents (Planner, Coder, Reviewer, Challenger, Synthesizer)
on your local machine using Ollama, or on free cloud tiers via Groq,
OpenRouter, and others.

Unlike Copilot or Cursor, Velune:

- Runs 100% locally with Ollama — no API key needed
- Remembers your codebase across sessions (persistent 5-tier memory)
- Reviews its own code using multiple specialized agents
- Works in any terminal on any project — no IDE required

---

## 60-second quickstart

### Option A — Local (Ollama, free, no key)

```bash
# 1. Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 2. Pull a model
ollama pull qwen2.5-coder:7b

# 3. Install Velune
pip install velune-cli

# 4. Initialize in your project
cd your-project
velune init

# 5. Start
velune
```

### Option B — Cloud free tier (Groq, fastest, no GPU needed)

```bash
pip install velune-cli
velune init --provider groq
velune setup        # enter your free Groq key
velune
```

Get a free Groq key at <https://console.groq.com/keys> — no credit card.

### Installing the `velune` command

```bash
pip install velune-cli
velune --version
```

If your shell reports **`velune: command not found`** (or, on Windows,
*"'velune' is not recognized…"*), the install succeeded but your Python
scripts directory is not on `PATH`. Two reliable fixes:

- **Recommended — install with [pipx](https://pipx.pypa.io/)** (isolated env, auto-managed PATH):

  ```bash
  pipx install velune-cli
  ```

- **Or run it as a module** (always works, no PATH changes needed):

  ```bash
  python -m velune --version
  python -m velune            # start the REPL
  ```

On Windows, a plain `pip install` puts the launcher in a per-user
`…\PythonXX\Scripts` folder; re-running the Python installer with **“Add
Python to PATH”** checked (or using `pipx`) resolves it permanently.

---

## Hardware requirements

| RAM    | GPU              | Can run local LLM? | Recommended setup            |
|--------|------------------|--------------------|------------------------------|
| < 8 GB | any              | ✗ No               | Use Groq free tier           |
| 8 GB   | integrated       | ⚠ 3B models only   | Groq + phi3-mini local       |
| 16 GB  | integrated       | ⚠ Slow (CPU only)  | Groq + 3B local              |
| 16 GB  | 6–8 GB VRAM      | ✓ 7B comfortable   | qwen2.5-coder:7b             |
| 32 GB  | 12+ GB VRAM      | ✓ 13B comfortable  | Full council local           |
| 36 GB  | Apple Silicon    | ✓ 27B comfortable  | Full council, Metal accel    |
| 64 GB  | 24 GB VRAM       | ✓ 70B capable      | Max power mode               |

Velune detects your hardware on startup and prints tier, GPU, and recommendations.
On underpowered machines it routes tasks to cloud providers automatically.

---

## Startup flow

Velune starts instantly and does no work until you ask for it. Repository
cognition (indexing) is **explicit and on-demand** — it never runs automatically
on launch.

```text
velune
    ↓
CLI opens instantly
    ↓
Connect a model      →  /model discover · /model connect <id> · /model use <id>
    ↓
Open a project       →  /project open <path> · /project status
    ↓
Run cognition        →  /cognition quick · /cognition standard · /cognition deep
```

---

## Interface

Velune features a modern, clean terminal interface designed for productivity:

- **Startup banner** shows your hardware tier, active model, and available providers
- **Responsive prompt** with intelligent context indicators (only displays when relevant)
- **Sophisticated color palette** using restrained styling for clarity
- **Intuitive command structure** with tab-completion for all slash commands
- **Session modes** for balancing speed vs. quality (Optimus / Normal / Godly)

---

## Providers

| Provider     | Type  | Cost          | Models                                        | Setup                        |
|--------------|-------|---------------|-----------------------------------------------|------------------------------|
| Ollama       | Local | Free          | Any pulled model                              | Install Ollama, pull a model |
| LM Studio    | Local | Free          | Any GGUF / MLX model                          | Launch LM Studio server      |
| Groq         | Cloud | Free tier     | Llama 3.3 70B, Mixtral, Gemma2                | `velune setup` → enter key   |
| OpenRouter   | Cloud | Pay-per-token | 100+ models                                   | `velune setup` → enter key   |
| OpenAI       | Cloud | Pay-per-token | GPT-4o, GPT-4o Mini                           | `velune setup` → enter key   |
| Anthropic    | Cloud | Pay-per-token | Claude Opus, Sonnet, Haiku                    | `velune setup` → enter key   |
| xAI (Grok)   | Cloud | Pay-per-token | Grok 2, Grok 2 Mini                           | `velune setup` → enter key   |
| Google       | Cloud | Free quota    | Gemini 2.0 Flash, 1.5 Pro/Flash               | `velune setup` → enter key   |
| Together AI  | Cloud | Pay-per-token | Llama 3.3 70B, Qwen 2.5 Coder, DeepSeek R1    | `velune setup` → enter key   |
| Fireworks AI | Cloud | Pay-per-token | DeepSeek R1, Qwen 2.5 Coder, Mixtral 8x22B    | `velune setup` → enter key   |
| Mistral      | Cloud | Pay-per-token | Mistral Large, Codestral, Mixtral             | `velune setup` → enter key   |
| DeepSeek     | Cloud | Pay-per-token | DeepSeek R1, DeepSeek Coder                   | `velune setup` → enter key   |
| Cohere       | Cloud | Pay-per-token | Command R+, Command R                         | `velune setup` → enter key   |
| NVIDIA NIM   | Cloud | Pay-per-token | Llama, Mistral, and other NIM models          | `velune setup` → enter key   |
| HuggingFace  | Cloud | Free/paid     | Open models via Inference API                 | `velune setup` → enter key   |

Keys are stored in your OS keyring — never in files, never in git.

---

## Commands

### CLI (terminal, before the REPL)

```bash
# Core
velune                    # Start the interactive REPL session
velune chat               # Same as above (explicit form)
velune run "<task>"       # Run a task non-interactively and exit
velune ask "<question>"   # Ask a one-shot question and exit
velune init               # Initialize Velune in a project directory

# Workspace & sessions
velune workspace init     # Index the current workspace
velune workspace status   # Show index freshness and file counts
velune workspace graph    # Render the workspace dependency graph
velune workspace list     # List all known workspaces
velune session list       # List saved chat sessions
velune session delete <id>  # Delete a saved session

# Setup & models
velune setup              # Configure API keys (stored in OS keyring)
velune models scan        # Discover all available local and cloud models
velune models list        # List discovered models
velune provider list      # Show all configured providers and their status
velune provider add       # Add a new provider interactively
velune config show        # Print effective velune.toml settings
velune config set <k> <v> # Write a setting to velune.toml

# Analytics & monitoring
velune usage              # Token usage and estimated cost for recent sessions
velune quota              # Check provider rate-limit and quota status
velune health             # Check provider reachability and response time

# Diagnostics
velune doctor             # Run full environment health check
velune logs               # View recent execution event log
velune logs live          # Follow new events as they are written
velune status             # Index freshness and workspace health snapshot
velune pipeline "<query>" # Trace a retrieval query through the search pipeline
velune daemon start       # Start the background Velune service
velune daemon stop        # Stop the background service
velune mcp serve          # Expose Velune's tool council as an MCP server
velune mcp connect <url> <name>  # Connect to an external MCP server and list tools
velune memory inspect     # Show memory tier sizes and record counts
velune memory clear       # Clear all memory tiers for the current workspace
```

### Inside the REPL

```text
─── Session ───────────────────────────────────────────────────────────────────
/help                    Show all commands and their aliases
/exit                    Exit Velune
/clear                   Clear the terminal screen (context is preserved)
/new [title]             Start a new conversation (project memory persists)

─── Workspace & Cognition ─────────────────────────────────────────────────────
/project open <path>     Open a directory as the active workspace
/project close           Close the current workspace
/project status          Show the active workspace and its cognition state
/project list            List recently-opened workspaces (reopen instantly)
/project add <path>      Remember a workspace without switching to it
/cognition quick         Fast scan — manifests only, no symbol index
/cognition standard      Build a full symbol index for the workspace
/cognition deep          Deep index (symbols + import graph + relationships)
/cognition status        Show index freshness and progress
/cognition cancel        Cancel a running cognition job
/cognition rebuild       Discard and rebuild the index from scratch

─── Council / Execution ───────────────────────────────────────────────────────
/run <task>              Execute a task through the Reasoning Council
/run --bg <task>         Submit task to background — prompt returns immediately
/council <task>          Force full council tier regardless of task complexity
/jobs                    List all background jobs (ID, status, phase, elapsed)
/jobs cancel <id>        Cancel a running background job
/dashboard               Live progress dashboard (jobs + alerts + provider health)

─── Models ────────────────────────────────────────────────────────────────────
/model [model-id]        Switch active model (arrow-key picker if no arg)
/model discover          Discover locally available models (e.g. Ollama)
/model connect <id>      Register/connect a model to the active session
/model use <id>          Set the active model by id
/model status            Show the active model and connection state
/model remove <id>       Remove a model from the registry
/models                  List all available models
/pull [model-id]         Download an Ollama model with live progress
/delete <model-id>       Delete a locally installed Ollama model
/councilmodel            Assign specific models to Planner / Coder / Reviewer roles
/bench [run]             View or run empirical model capability benchmarks

─── Session Modes ─────────────────────────────────────────────────────────────
/optimus                 Speed mode — instant tier, smallest model, 4k context
/godly                   Max power — full council, largest model, 128k context
/normal                  Return to balanced mode (auto-tier, 16k context)
/mode                    Show current mode settings and active council tier

─── Memory & Context ──────────────────────────────────────────────────────────
/memory [clear|stats]    Inspect or clear memory tiers
/session                 Interactive session picker (list / resume / save / export)
/context                 Show context window usage for the current conversation
/graph                   Render a tree of knowledge graph entities

─── Diffs & Editing ───────────────────────────────────────────────────────────
/diff                    Show uncommitted file changes from the last council run
/undo                    Revert the last Velune-generated git commit (keeps changes staged)
/hunk                    Toggle hunk-by-hunk review mode for edits
/approve [safe|ask|block] Set tool/command approval gate

─── Git Integration ───────────────────────────────────────────────────────────
/push [--force]          Push current branch to remote origin
/pr <title>              Create a pull request / merge request on GitHub or GitLab
/issue <number>          Fetch a GitHub/GitLab issue and inject it as context
/sandbox [docker|status] Show or switch sandbox type (subprocess or Docker)

─── Code Intelligence ─────────────────────────────────────────────────────────
/lint [file]             Lint a Python file and display diagnostic output
/refactor <file>         Detect code smells and suggest refactoring targets
/typify <file>           Suggest type hints for unannotated functions

─── MCP & Plugins ─────────────────────────────────────────────────────────────
/mcp [servers|tools|resources|connect|disconnect]  Inspect MCP servers and tools
/plugin [list|enable|disable|reload|show]          Manage declarative plugins

─── Diagnostics ───────────────────────────────────────────────────────────────
/doctor                  Run environment health checks
/config                  Show current configuration settings
/stats                   Session statistics: tokens, cost, turns, uptime
/history                 Show REPL command execution history
/hooks                   List active lifecycle hooks and their configuration
```

Tab-completion is active for all `/` commands and for model IDs (type `/model` then Space to trigger).

The status bar shows `⚙ N bg` for active background jobs and `⚠ N` for unread proactive alerts. Alerts drain automatically after each prompt and are printed as Rich panels above the input line.

---

## Architecture overview

```text
velune/
├── cli/              REPL, slash commands, banner, autocomplete, session manager
│   ├── commands/     Typer subcommands (workspace, session, models, doctor, mcp, …)
│   ├── display/      Live dashboards and council pipeline view
│   └── rendering/    Rich error panels and markdown streaming
├── providers/        15 provider adapters (Ollama, Groq, OpenAI, Anthropic, Mistral, …)
│   ├── adapters/     Per-provider inference + streaming implementations
│   └── discovery/    Model catalog discovery for each provider
├── cognition/        Council: Planner → Coder → Reviewer → Challenger → Synthesizer
│   └── council/      DebateSession, CouncilRunner, per-role agents, tier classifier
├── memory/           5-tier: working → episodic → semantic → graph → lineage
├── proactive/        Alert store + watcher (CognitiveBus event subscriptions)
├── repository/       AST indexing, import graph, blast-radius estimator, .veluneignore
├── retrieval/        Hybrid retrieval: BM25 + vector + graph, cross-encoder reranker
├── execution/        Managed execution (allowlist + limits), diff preview, rollback
│   └── edit_formats/ Diff format parsers (unified, search-replace, XML, JSON)
├── analysis/         Code intelligence: linting, code-smell detection, type inference
├── integrations/     GitHub and GitLab REST clients (push, PR, issues)
├── hooks/            Lifecycle hook dispatcher and executor (pre/post tool events)
├── observability/    Context reports, execution trace log, workspace dependency graph
├── mcp/              MCP server + client; stdio / SSE / HTTP / WebSocket transports
├── hardware/         Hardware detection, tier classification, GPU probe
├── telemetry/        Token tracking, cost estimation, latency profiling
├── models/           Model registry, capability scoring, specializations
├── context/          Context window tracking, token counting, extractive compression
├── orchestration/    ContextOrchestrationEngine — wires intent → council → output
├── core/             Loop detector, retry policy, task/job registry, error types
├── kernel/           Bootstrap, lifecycle coordinator, service container
├── daemon/           Background Velune service (server + IPC transport)
├── tools/            File-system, git, web-fetch, and terminal tool implementations
└── plugins/          Declarative plugin loader, SKILL.md injection, hook wiring
```

---

## Memory system

Velune maintains five memory tiers across sessions:

1. **Working** — current conversation turns (in-process, TTL-evicted)
2. **Episodic** — session history (SQLite, persisted to `~/.velune/`)
3. **Semantic** — vector search over past interactions (local LanceDB and Qdrant)
4. **Graph** — repository structure and symbol relationships
5. **Lineage** — decision history, what was tried and why

This means "fix the auth issue from yesterday" actually works —
Velune retrieves recent sessions, git changes, and related context
to reconstruct intent without you explaining it again.

---

## Session modes

| Mode    | Command    | Council tier | Model    | Context cap  |
|---------|------------|--------------|----------|--------------|
| Normal  | `/normal`  | auto         | current  | 16 k tokens  |
| Optimus | `/optimus` | instant      | smallest | 4 k tokens   |
| Godly   | `/godly`   | full         | largest  | 128 k tokens |

Switch modes at any time mid-session. The prompt badge updates immediately.

---

## MCP integration

Velune works as both an MCP **server** and an MCP **client**:

- **Server** (`velune mcp serve`) — exposes Velune's local tool council over
  stdio so Claude Desktop, VS Code, and other MCP-capable editors can call
  Velune's models without sending your code to a third party.
- **Client** (`velune mcp connect <url> <name>`) — connects to any external MCP
  server, lists its tools, and makes them available inside the REPL via `/mcp`.
- **Transports** — stdio, SSE, HTTP, and WebSocket (`ws://` / `wss://`) are all
  supported. Servers can also be declared in `.mcp.json` and loaded automatically.

Outbound connections to external MCP servers are trust-gated — see
[MCP trust gating](docs/SECURITY.md#mcp-trust-gating) in the security policy.

---

## Windows

Velune runs natively on Windows (supporting native command execution sandboxing, local Ollama integration, and keyring credentials). It can also run via WSL2 if preferred.

---

## Project docs

| Doc | What's inside |
| --- | --- |
| [docs/SECURITY.md](docs/SECURITY.md) | Security posture, trust boundaries, reporting |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | Dev setup and contribution workflow |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Full version history |

---

## Optional extras

The default install is intentionally lean and pure-python-friendly so it
resolves fast and cleanly on every platform. Heavy or feature-specific
dependencies live in extras — every feature that needs one **degrades
gracefully** when it is absent (e.g. semantic search becomes a no-op, but
lexical search and chat keep working).

| Extra | Installs | Enables |
| --- | --- | --- |
| `[rag]` | `lancedb`, `pyarrow`, `qdrant-client` | Semantic memory + vector retrieval (large compiled wheels) |
| `[parsing]` | `tree-sitter` + grammars | Tree-sitter source parsing for deep repository cognition |
| `[telemetry]` | `opentelemetry-*` | Export spans/metrics to an OTLP collector |
| `[git]` | `gitpython` | Git provider tools (push / PR / issue) and richer git context |
| `[gguf]` | `gguf` | GGUF file metadata reading — safe, no transitive risk |
| `[docker]` | `docker` | Docker sandbox for isolated code execution |
| `[all]` | everything above | Full-featured install |
| `[dev]` | Test/lint tools | For contributors |

```bash
pip install velune-cli            # lean base (Ollama, cloud providers, chat, lexical search)
pip install 'velune-cli[rag]'     # + semantic memory & vector retrieval
pip install 'velune-cli[all]'     # + every optional feature
```

> The former `[llamacpp]` extra has been **permanently removed**:
> `llama-cpp-python` pulls in `diskcache ≤ 5.6.3` (unsafe pickle
> deserialization, no patched version). Install `llama-cpp-python` manually,
> in a trusted single-user environment only, if you accept that risk.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

Before opening a PR:

```bash
pip install -e ".[dev]"
ruff check velune/
pytest tests/ -q
```

Report security issues via
[GitHub Security Advisories](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new)
— not public issues.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

Copyright 2026 Surya HA
