# Velune

> Local-first multi-model AI developer CLI. Council-based agents,
> persistent memory, repository cognition.
> No cloud required. No quota. No lock-in.

[![PyPI](https://img.shields.io/pypi/v/velune)](https://pypi.org/project/velune/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
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
pip install velune

# 4. Initialize in your project
cd your-project
velune init

# 5. Start
velune
```

### Option B — Cloud free tier (Groq, fastest, no GPU needed)

```bash
pip install velune
velune init --provider groq
velune setup        # enter your free Groq key
velune
```

Get a free Groq key at <https://console.groq.com/keys> — no credit card.

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

## Interface

Velune features a modern, clean terminal interface designed for productivity:

- **Startup banner** shows your hardware tier, active model, and available providers
- **Responsive prompt** with intelligent context indicators (only displays when relevant)
- **Sophisticated color palette** using restrained styling for clarity
- **Intuitive command structure** with tab-completion for all slash commands
- **Session modes** for balancing speed vs. quality (Optimus / Normal / Godly)

---

## Providers

| Provider              | Type  | Cost           | Models                               | Setup                            |
|-----------------------|-------|----------------|--------------------------------------|----------------------------------|
| Ollama                | Local | Free           | Any pulled model                     | Install Ollama, pull a model     |
| Groq                  | Cloud | Free tier      | Llama 3.3 70B, Mixtral, Gemma2       | `velune setup` → enter key       |
| OpenRouter            | Cloud | Pay-per-token  | 100+ models                          | `velune setup` → enter key       |
| OpenAI                | Cloud | Pay-per-token  | GPT-4o, GPT-4o Mini                  | `velune setup` → enter key       |
| Anthropic             | Cloud | Pay-per-token  | Claude Opus, Sonnet, Haiku           | `velune setup` → enter key       |
| xAI (Grok)            | Cloud | Pay-per-token  | Grok 2, Grok 2 Mini                  | `velune setup` → enter key       |
| Google Gemini         | Cloud | Free quota     | Gemini 2.0 Flash, 1.5 Pro/Flash      | `velune setup` → enter key       |

Keys are stored in your OS keyring — never in files, never in git.

---

## Commands

### CLI (before the REPL starts)

```bash
velune              # Start the persistent REPL session
velune init         # Initialize Velune in a project
velune setup        # Configure API keys securely (stored in OS keyring)
velune doctor       # Check hardware, providers, dependencies
velune models scan  # Discover all available local and cloud models
```

### Inside the REPL

```text
/run <task>              Execute a task through the council
/model                   Switch active model (arrow-key picker)
/models                  List all available models
/optimus                 Speed mode — instant tier, smallest model
/godly                   Max power — full council, largest model
/normal                  Return to balanced mode
/mode                    Show current mode settings
/memory                  Inspect memory tiers
/session save            Save current session
/session list            List saved sessions
/session resume <id>     Resume a session
/usage                   Token count and cost for this session
/context                 Context window usage indicator
/diff                    Show pending file changes
/doctor                  Run health checks
/help                    Show all commands
/clear                   Clear screen and context
/exit                    Exit Velune
```

Tab-completion is active for all `/` commands and for model IDs (type `/model` then space to trigger).

---

## Architecture overview

```text
velune/
├── cli/              REPL, slash commands, banner, autocomplete, session manager
├── providers/        Ollama, Groq, OpenAI, Anthropic, xAI, Google, OpenRouter
├── cognition/        Council: Planner + Coder + Reviewer + Challenger + Synthesizer
├── memory/           5-tier: working → episodic → semantic → graph → lineage
├── repository/       AST indexing, dependency graph, .veluneignore
├── execution/        Sandbox, diff preview, rollback, cancellation
├── hardware/         Hardware detection, tier classification, GPU probe
├── telemetry/        Token tracking, cost estimation, latency profiling
├── models/           Model registry, capability scoring, specializations
├── context/          Context window tracking, extractive compression
├── kernel/           Bootstrap, lifecycle coordinator, service container
└── plugins/          Plugin API, sandbox, hook registry
```

---

## Memory system

Velune maintains five memory tiers across sessions:

1. **Working** — current conversation turns (in-process, TTL-evicted)
2. **Episodic** — session history (SQLite, persisted to `~/.velune/`)
3. **Semantic** — vector search over past interactions (local Qdrant)
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

Velune exposes an MCP server so that Claude Desktop and VS Code can call
Velune's local model council as a tool — giving cloud-based editors access
to local hardware without sending your code to a third party.

See [`docs/mcp.md`](docs/mcp.md) for configuration examples and the full tool reference.

---

## Windows

Velune runs on Windows via WSL2. Native Windows support is planned.

See [WINDOWS.md](WINDOWS.md) for the complete WSL2 setup guide.

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
[GitHub Security Advisories](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new) —
not public issues.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

Copyright 2026 Surya HA
