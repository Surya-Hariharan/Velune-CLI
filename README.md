# Velune CLI

Local-first, multi-model AI developer CLI with council-based agents, persistent
memory, and repository cognition.

[![PyPI](https://img.shields.io/pypi/v/velune-cli)](https://pypi.org/project/velune-cli/)
[![Python](https://img.shields.io/pypi/pyversions/velune-cli)](https://pypi.org/project/velune-cli/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/Surya-Hariharan/Velune-CLI/ci.yml?branch=main&label=CI)](https://github.com/Surya-Hariharan/Velune-CLI/actions/workflows/ci.yml)

[Installation](#installation) · [Quickstart](#quickstart) · [Configuration](#configuration) · [Commands](#commands) · [Documentation](#documentation) · [Contributing](#contributing) · [Security](#security)

## Project status

Velune CLI is **beta software** (version 0.x). Command names, configuration keys
and on-disk formats may change between minor releases; changes are recorded in
the [changelog](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CHANGELOG.md).
The documentation on the `main` branch can describe changes that are not in the
latest PyPI release yet: run `velune --version` and compare it with the
changelog.

Experimental or opt-in capabilities are labelled as such in this document. They
are `velune native` (native orchestration engine), the `deliberative` council
engine, the Docker sandbox, and the optional `rag` and `parsing` extras.

## What Velune is

Velune CLI is a terminal application for working on a codebase with language
models. You talk to it in a full-screen REPL (`velune`) or run one-shot commands
(`velune ask`, `velune run`). It can read your repository, propose and apply
edits, and run commands, with each action gated by an approval policy you
control.

It can use models running on your machine (Ollama, LM Studio, an
OpenAI-compatible server) or hosted models from cloud providers you configure.
Which of the two receives your prompts is your choice, and Velune does not
switch from local to cloud on its own (see
[Local and cloud providers](#local-and-cloud-providers)).

## Core capabilities

| Capability | Summary | Status |
| :--- | :--- | :--- |
| Multi-provider chat | 18 provider adapters, local and cloud; HTTP APIs are called with `httpx` rather than vendor SDKs | Implemented |
| Council | Planner, Coder, Reviewer, Challenger and Synthesizer agents; the council tier is chosen per task or pinned with `/fast`, `/normal`, `/max` | Implemented |
| Repository cognition | Explicit, on-demand indexing: file scan, symbol index, import graph, blast-radius estimates | Implemented |
| Persistent memory | Working, episodic, semantic, graph and lineage tiers, stored per workspace | Implemented; semantic tier needs the `rag` extra |
| Hybrid retrieval | BM25 keyword search, vector search and graph signals, with a heuristic reranker | Implemented; vector search needs the `rag` extra |
| Tools and execution | File, git, web-fetch and command tools behind execution modes, diff preview and rollback | Implemented |
| MCP | Runs as an MCP server over stdio and as an MCP client over stdio, SSE, HTTP and WebSocket | Implemented |
| Native orchestration engine | `velune native`: read-only, per-call approval, no fallback to other providers | Experimental, off by default |
| Docker sandbox | Runs agent commands in a per-session container | Optional, off by default |

## Architecture

Velune is a single Python process. The CLI layer builds a runtime (configuration,
providers, models, then the repository, memory, retrieval, execution and agent
subsystems) and routes each request through the provider layer. Heavy subsystems
load lazily so that `velune --version` and `--help` start quickly.

```mermaid
flowchart LR
    User([You]) --> CLI["CLI and REPL<br/>velune/cli"]
    CLI --> Agents["Council and tool loop<br/>velune/cognition, velune/orchestration"]
    Agents --> Ctx["Context assembly<br/>velune/context"]
    Ctx --> Mem["Memory<br/>velune/memory"]
    Ctx --> Ret["Retrieval<br/>velune/retrieval"]
    Ret --> Repo["Repository index<br/>velune/repository"]
    Agents --> Providers["Providers<br/>velune/providers"]
    Agents --> Exec["Execution and permissions<br/>velune/execution, velune/permissions"]
    Exec --> FS[(Workspace files)]
    Providers --> Models[("Local or cloud models")]
    Agents --> MCP["MCP client and server<br/>velune/mcp"]
```

| Package | Responsibility |
| :--- | :--- |
| `velune/cli` | Typer commands, full-screen REPL, slash commands, themes, onboarding |
| `velune/providers`, `velune/models` | Provider adapters, model discovery, credentials, model registry and role mapping |
| `velune/cognition` | Council agents, debate, and tier classification |
| `velune/orchestration` | Native tool-calling loop and the experimental native engine |
| `velune/memory`, `velune/retrieval` | Memory tiers, SQLite and vector stores, hybrid retrieval |
| `velune/repository`, `velune/intelligence`, `velune/knowledge` | Scanner, symbol and import index, incremental re-indexing, knowledge graph |
| `velune/execution`, `velune/permissions`, `velune/tools` | Command runner, path guard, diff preview, rollback, approval policy, tool implementations |
| `velune/mcp`, `velune/hooks`, `velune/plugins`, `velune/resources` | MCP server and client, lifecycle hooks, declarative plugins, Docker and database connectors |
| `velune/recovery`, `velune/lifecycle` | Backup, restore and crash recovery; update, repair and uninstall |

The module boundaries and bootstrap sequence are described in
[docs/development.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/development.md).

## Installation

### Install Velune CLI

**macOS, Linux, WSL**

```bash
curl -fsSL https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/scripts/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/scripts/install.ps1 | iex
```

The installer puts Velune in its own isolated environment using
[uv](https://docs.astral.sh/uv/). It does not need Python to be installed: if
Python is missing or too old, uv downloads a private copy. It adds `velune` to
your `PATH`, and running the same command again upgrades the installation. The
scripts are fetched from the `main` branch and uv's own installer is fetched from
`astral.sh`; read the scripts first if that trust is not acceptable.

<details>
<summary>Install with pipx, uv or pip instead</summary>

```bash
pipx install velune-cli          # isolated, PATH managed
uv tool install velune-cli       # isolated, PATH managed
python -m pip install velune-cli # into the current Python environment
```

Use `python -m pip`, not bare `pip`, so the package goes into the same Python you
will run it with. Plain pip needs Python 3.10 or newer. Common problems:

- **`Could not find a version that satisfies the requirement velune-cli`** (with a
  `Requires-Python` note): your Python is older than 3.10. Check `python --version`.
- **`externally-managed-environment`** (Debian, Ubuntu, Homebrew Python): the OS
  protects its Python. Use the installer, `pipx`, `uv tool`, or a virtualenv.
- **Conflicts with other tools' dependencies:** pip keeps one copy of each package
  per environment. An isolated install avoids this. If Velune detects an
  incompatible dependency at startup, it names the package and the fix command.
- **`velune: command not found`:** your Python scripts directory is not on `PATH`.
  Use `python -m velune`, or reinstall with `pipx` or the installer.

On the platforms marked **Tested** below, the core dependencies install from
prebuilt wheels for CPython 3.10 to 3.14, so no compiler is needed. CI checks
this, including at the lowest allowed version of each dependency.

</details>

#### Platform support

| Platform | Status |
|---|---|
| Windows 10/11, x86-64 | **Tested** in CI |
| macOS, Apple Silicon (arm64) | **Tested** in CI |
| Linux, x86-64, glibc (Ubuntu, Debian, Fedora, …) | **Tested** in CI |
| Linux, arm64, glibc | Not tested. Prebuilt wheels exist for the core dependencies. |
| macOS, Intel (x86-64) | **Unverified.** In our lock file (`uv.lock`) the `cryptography` release has no macOS x86-64 wheel, so installing the base CLI may need a Rust toolchain to build it. The installer does not block it and it may work; we have not tested it or checked what PyPI currently offers. The optional `[rag]` extra is unavailable. |
| Alpine / other musl Linux | Not tested. Prebuilt wheels exist for the core dependencies. |
| WSL 2 | Not tested separately; treated as Linux. |
| **Windows on ARM64** | **Unsupported.** The installer stops before downloading anything. Required dependencies (`cryptography`, `tiktoken`) publish no Windows ARM64 wheels, so an install would need a Rust and C++ build toolchain. |
| 32-bit systems, BSD, other architectures | **Unsupported.** The installer stops before downloading anything. |

**Tested** means a CI job installs and runs it. **Not tested** means the
installer lets you proceed and our lock file has prebuilt wheels for the core
dependencies, so we expect it to work, but no CI job covers it. **Unverified**
means we found a gap that could make the install fail or need build tools, and
have not confirmed either way. **Unsupported** means the installer refuses to
run. None of these is a guarantee.

#### Verify the install

```bash
velune --version
velune doctor
```

`velune doctor` shows the Velune version, Python interpreter, operating system,
and where Velune is installed and keeps its data. It checks the installation and
ends with "✓ Core installation is healthy." or a list of problems. Optional
integrations (provider keys, a local Ollama, extras such as `[rag]`) are reported
but are never counted as installation failures. The exit code is non-zero only
when the core installation has a problem. `velune doctor --json` prints the same
report as JSON, with home-directory paths shortened to `~`, so it can be pasted
into a bug report.

### Optional extras

The base install is intentionally small. Features that need heavier or compiled
dependencies live in extras and turn themselves off when the extra is missing
(for example, semantic search is disabled while keyword search keeps working).
`velune doctor` reports which are available.

| Extra | Installs | Enables |
| :--- | :--- | :--- |
| `rag` | `lancedb`, `pyarrow`, `qdrant-client` | Semantic memory and vector retrieval |
| `parsing` | `tree-sitter` and language grammars | Tree-sitter parsing for repository cognition; without it, lighter heuristics are used |
| `telemetry` | `opentelemetry-*` | Export spans and metrics to an OTLP collector |
| `gguf` | `gguf` | Reading GGUF file metadata |
| `docker` | `docker` | Docker sandbox for command execution |
| `git` | nothing | Kept for compatibility; git tools use the `git` executable directly |
| `all` | all of the above | Every optional feature |
| `dev` | test, lint and build tools | Contributors only |

```bash
pip install 'velune-cli[rag]'
pip install 'velune-cli[all]'
uv tool install --force 'velune-cli[rag]'   # add an extra to an installer or uv tool install
pipx install --force 'velune-cli[rag]'      # add an extra to a pipx install
```

The `rag` extra depends on LanceDB, which publishes prebuilt wheels only for
Windows x86-64, macOS Apple Silicon and glibc Linux (x86-64 and arm64), so it
cannot be installed on musl Linux, Intel Macs or Windows ARM64. The installer
scripts install the base CLI only.

The former `llamacpp` extra was removed because `llama-cpp-python` depends on
`diskcache`, which deserializes cache entries with `pickle` and has no patched
release. The llama.cpp adapter remains in the code and is available only if you
install `llama-cpp-python` yourself, which is not recommended outside a trusted
single-user environment.

## Quickstart

### With a local model (Ollama)

```bash
curl -fsSL https://ollama.com/install.sh | sh   # or install Ollama from ollama.com
ollama pull qwen2.5-coder:7b

cd your-project
velune init
velune
```

`velune init` creates the project configuration and `.veluneignore`, and builds
the repository index (`--no-index` skips the index). Indexing is otherwise
explicit and on demand (`/index quick|standard|deep` in the REPL); it never runs
automatically when Velune starts.

To use Ollama on another machine or port, set `OLLAMA_HOST` (for example
`OLLAMA_HOST=192.168.1.20:11434`). Setup, discovery, health checks and chat all
use it.

### With a cloud provider (Groq)

```bash
velune init --provider groq
velune setup        # prompts for your Groq API key
velune
```

Groq offers a free tier; create a key at <https://console.groq.com/keys>. Check
the result at any time with `velune doctor`.

### Models on an external drive

Run `/model locate` (or `/model register <path>`) on the Ollama store folder, the
one containing `manifests/` and `blobs/`. Velune remembers it. If the drive is
unplugged, the model stays listed as unavailable with the reason; reconnect it
and run `/model recheck` (or send your next message). If the drive returns under
a different path, use `/model relocate <model> <new-path>`. A single `.gguf` file
or a safetensors folder can be registered with `/model register <path>`; that
imports it into Ollama, which copies the file, so Velune shows the size and asks
first.

### Hardware guidance

Velune detects RAM, GPU and accelerator at startup, classifies the machine into a
tier, and prints warnings and suggestions. These are suggestions only; Velune does
not move your prompts to a cloud provider because of them.

| RAM | Accelerator | Local models |
| :--- | :--- | :--- |
| under 8 GB | any | Not practical for local models; use a cloud provider |
| 8 GB | integrated | About 3B-parameter models |
| 16 GB | integrated | 3B models run well; 7B models run slowly |
| 16 GB | 6 GB or more VRAM | 7B models |
| 32 GB | 12 GB or more VRAM | 13B models |
| 64 GB | 24 GB or more VRAM | 70B-class models |
| Apple Silicon, 16 GB or more | unified memory | 13B models |
| Apple Silicon, 36 GB or more | unified memory | 70B-class models |

## Provider and model configuration

| Provider | Kind | Notes | Connect with |
| :--- | :--- | :--- | :--- |
| Ollama | Local | Any pulled model | `/model discover` |
| LM Studio | Local | Server must be running | `/connect lmstudio` |
| OpenAI-compatible | Local server | vLLM, LocalAI, text-generation-webui, and similar; set the base URL | `/connect openai-compat` |
| llama.cpp | Local, in-process | Needs `llama-cpp-python` installed manually (see [Optional extras](#optional-extras)) | `/connect llamacpp` |
| Groq | Cloud | Free tier available | `/connect groq` |
| OpenRouter | Cloud | Many models through one key | `/connect openrouter` |
| OpenAI | Cloud | Chat models your key can use | `/connect openai` |
| Anthropic | Cloud | Claude models | `/connect anthropic` |
| xAI | Cloud | Grok models | `/connect xai` |
| Google | Cloud | Gemini models | `/connect google` |
| Together AI | Cloud | Open models | `/connect together` |
| Fireworks AI | Cloud | Open models | `/connect fireworks` |
| Mistral | Cloud | Mistral and Codestral models | `/connect mistral` |
| DeepSeek | Cloud | DeepSeek models | `/connect deepseek` |
| Cohere | Cloud | Command models | `/connect cohere` |
| Meta (Llama API) | Cloud | First-party Llama models | `/connect meta` |
| NVIDIA NIM | Cloud | Chat models from build.nvidia.com; `nvapi-…` key | `/connect nvidia` |
| Hugging Face | Cloud | Inference API | `/connect huggingface` |

Pricing and free-tier terms belong to each provider and change over time; check
the provider. Model lists for OpenAI, Anthropic, Google, Groq and NVIDIA NIM are
read from the provider when available. If a listing fails (offline, rate limit,
a key without list permission), Velune keeps the last known list and reports the
reason.

`velune setup`, `velune provider add <id>` and `/connect` walk you through adding
a provider. `velune provider test <id>` and `velune doctor providers` check it.

### Credentials

API keys are stored AES-GCM-encrypted in your user configuration directory. The
encryption key is kept in the operating-system keyring, or derived from
`VELUNE_MASTER_PASSPHRASE` on machines without one. Keys are not written into the
workspace. See [SECURITY.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/SECURITY.md#credential-protection)
for the details and limits of this protection.

A key saved through Velune takes precedence over a provider environment variable
such as `OPENAI_API_KEY`; set `VELUNE_PREFER_ENV_KEYS=1` to reverse that.
`/model status` shows which key is in effect without printing it. If a provider
cannot be reached when you add a key, the key is still saved and checked again
later; only a key the provider rejects is refused.

### Local and cloud providers

- **Local providers** send prompts to the endpoint you configured. That is your
  own machine by default, but a remote `OLLAMA_HOST` or an OpenAI-compatible
  server on another host receives your prompts and repository context.
- **Cloud providers** receive the prompt and whatever repository or memory
  context Velune includes in the request. Their retention and training policies
  are set by the provider, not by Velune.
- A council seat whose local model fails is not retried on a cloud provider unless
  you set `providers.allow_cloud_fallback_from_local = true`. Review
  `providers.fallback_providers` before enabling it.
- Velune has no analytics service and sends no usage data to its maintainers.
  Network traffic is limited to the providers, MCP servers, git hosts and web
  pages you use, plus the PyPI request made when you run `velune update`.

## Repository interaction and file operations

Velune can read, create, edit, move and delete files and run commands in your
project, depending on the [execution mode](#execution-modes-and-permissions).

- **Indexing** is explicit: `/index quick|standard|deep|status|rebuild` in the
  REPL, or `velune project init` from the shell (`velune status` reports index
  freshness). Memory and retrieval stores are kept per workspace in your user
  data directory, not in the repository; a project's `.velune/` folder holds
  plans and small local files. `.veluneignore` excludes paths from indexing, and
  its default template excludes `.env`, `*.pem`, `*.key` and similar files.
- **Path guard.** Filesystem and git tools resolve paths, including symlinks, and
  refuse anything outside the workspace unless you approve it.
- **Diff preview.** In Manual mode every change is shown as a diff and applied
  only after you approve it (`/hunk` enables hunk-by-hunk review).
- **Snapshots and undo.** Plan-driven changes take a git stash or branch snapshot
  first and are restored if execution fails. `/undo` reverts the last
  Velune-authored commit.
- **Commits.** Velune does not commit council edits unless you set
  `execution.council_auto_commit = true`.

## Agent orchestration

- **Council.** For larger tasks, `/run <task>` and `/council <task>` run Planner,
  Coder, Reviewer, Challenger and Synthesizer agents. The council tier (`instant`,
  `minimal`, `standard`, `full`) is classified from the task and from how many
  files depend on the code involved, unless you pin it.
- **Session modes.** These trade speed for depth. Switch at any time.

  | Mode | Command | Council tier | Model | Context cap |
  | :--- | :--- | :--- | :--- | :--- |
  | Normal | `/normal` | Auto | Current | 16k tokens |
  | Fast | `/fast` | Instant | Smallest | 4k tokens |
  | Max | `/max` | Full | Largest | 128k tokens |

- **Native tool loop.** In normal chat, models that support function calling can
  call workspace tools. Read-only tools run without a prompt; write and execute
  tools follow the execution mode.
- **Native orchestration engine (experimental).** `velune native "<task>"` runs a
  single read-only task with per-call approval, and never falls back to another
  provider or model. It is disabled until you set
  `VELUNE_ORCHESTRATION__ENGINE=on` or `[orchestration] engine = "on"`. See
  [docs/native-engine.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/native-engine.md).
- **Limits.** A council is several calls to the same or different models. It
  increases cost and latency, and it does not guarantee correct output: review
  diffs before accepting them.

## Memory and retrieval

Memory has five tiers, stored per workspace:

1. **Working:** current conversation turns, in process.
2. **Episodic:** session history in SQLite.
3. **Semantic:** vector search over past interactions (LanceDB and Qdrant client; needs the `rag` extra).
4. **Graph:** entities and relationships observed in conversations and executions.
5. **Lineage:** decisions and what was tried.

Retrieval combines BM25 keyword search, vector search and graph signals, then
reranks with a heuristic reranker. Without the `rag` extra, vector search is
disabled and keyword and graph retrieval continue to work. Inspect and manage
memory with `velune memory stats|inspect|clear|compact` and `/memory`, and trace
a query with `velune pipeline trace "<query>"`. Each workspace has its own memory
store, keyed by the folder's absolute path, so same-named folders in different
locations are kept apart.

## Execution modes and permissions

Press Shift+Tab to cycle modes, use the slash commands, or start with
`velune --mode manual|plan|auto`.

| Mode | Command | Behavior |
| :--- | :--- | :--- |
| Manual (default) | `/manual` | Reads and analyzes freely and asks before every change. Related changes are shown together for one approval. |
| Plan | `/plan [task]` | Investigates and writes a plan to `.velune/plans/<name>.md` without changing project files. Reply "approve" to run it. |
| Auto | `/auto` | Creates, edits and deletes files and runs commands inside the project without asking, iterating until the task is done. |

In every mode Velune still asks before touching anything outside the project
folder, before high-risk operations (for example `git reset --hard`, `git clean`,
force-push, recursive deletes, global installs, environment or registry changes),
and before reading or changing secrets such as `.env` files, keys or `~/.ssh`.
Commands such as `sudo` and disk formatting are not run. These rules are enforced
in the tool layer rather than by instructions to the model, and each decision is
logged (`velune trace --type permission`).

`/approve safe|ask|block` separately controls whether tool calls in the current
session need confirmation. Commands run as your user through a managed runner
(allowlisted executables, no shell, resource limits). That is process management,
not OS-level isolation; for isolation, enable the optional Docker sandbox
(`execution.docker_sandbox = true`). See
[SECURITY.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/SECURITY.md).

## MCP integration

- **Server:** `velune mcp serve` exposes Velune's tools to MCP-capable clients
  such as Claude Desktop or editors, over stdio. Tools are read-only unless
  `VELUNE_MCP_ALLOW_MUTATIONS=1` is set.
- **Client:** `velune mcp connect <url> <name>` or `/mcp connect` connects to an
  external MCP server and makes its tools available in the REPL. Stdio, SSE, HTTP
  and WebSocket transports are supported. Servers can also be declared in
  `.mcp.json`, which is honored only in directories you have trusted
  (`velune trust add`).
- Outbound connections are validated: cloud-metadata and link-local addresses are
  always blocked, and `[mcp] allowed_hosts` can restrict connections to a list of
  hosts.

See [docs/mcp.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/mcp.md).

## Configuration

Settings come from `velune.toml`, which Velune looks for in the workspace, then in
parent directories, then at `~/.velune/velune.toml`. A value in the file takes
precedence over a `VELUNE_*` environment variable (nested keys use `__`, for
example `VELUNE_EXECUTION__MODE=plan`), and both override the built-in defaults.
Read and change values with `velune config show|get|set`.

The repository's [velune.toml](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/velune.toml)
lists every available setting at its default, with explanations.

```toml
[providers]
default_provider = "groq"

[execution]
mode = "manual"   # "manual", "plan" or "auto"
```

Project-level `base_url` overrides, `.mcp.json` servers and hooks are honored only
in trusted directories (`velune trust add|list|forget`), so a repository you have
just cloned cannot redirect your API traffic or start processes by shipping a
configuration file.

## Platform compatibility

- **Python:** 3.10 to 3.14 (CPython). CI runs the test suite on every combination
  of these versions with Windows, macOS and Linux.
- **Operating systems and architectures:** see [Platform support](#platform-support).
- **Windows:** Velune runs natively and stores credential keys in the operating-system
  keyring. Command execution uses the same managed runner as on other platforms
  (it is process management, not OS-level isolation). The PowerShell installer and
  the test suite run on Windows in CI. WSL 2 is treated as Linux.
- **Terminal modes:** bare `velune` opens the full-screen REPL; `velune chat`
  provides an older line-based mode.

## Commands

Run `velune --help`, or `velune <command> --help`, for the full signature of each
command.

| Group | Commands |
| :--- | :--- |
| Core | `velune` (REPL), `chat`, `run "<task>"`, `ask "<question>"`, `native "<task>"` (experimental), `init`, `onboard` |
| Workspace and sessions | `project init\|status\|graph\|tree\|list\|open\|resume\|explain\|forget` (alias `workspace`), `session list\|resume\|show\|delete\|import\|rename\|search\|archive\|unarchive\|export` |
| Setup and models | `setup`, `models scan\|list\|refresh\|pull\|delete\|assign\|use\|benchmark\|health\|show`, `provider list\|add\|remove\|test\|models\|status\|api\|edit\|inspect\|default\|backup\|restore\|repair`, `config show\|get\|set`, `trust add\|list\|forget` |
| Analytics and monitoring | `usage`, `quota`, `health` |
| Diagnostics | `doctor [providers\|network]`, `logs [recent\|live]`, `status`, `pipeline trace "<query>"`, `daemon start\|stop\|status`, `mcp serve\|connect`, `memory stats\|inspect\|clear\|compact` |
| Lifecycle | `update [--check]`, `repair`, `uninstall`, `purge-data` |
| Trust and recovery | `backup`, `restore`, `recover` |

`update --check` is read-only and works for any install. Applying an update, and
`repair` and `uninstall`, change only a uv-installed copy and refuse other install
methods. They leave your data in place; only `purge-data` deletes it. The
exit codes, platform notes and verification status are in
[docs/lifecycle.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/lifecycle.md),
and what `purge-data` can delete is listed in
[docs/data-inventory.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/data-inventory.md).

Inside the REPL there are 55 slash commands in 12 categories, such as `/help`,
`/run`, `/council`, `/index`, `/connect`, `/model`, `/memory`, `/mcp`, `/diff`,
`/undo`, `/approve`, `/doctor` and `/backup`. Press Tab to complete commands and
model IDs, and run `/help` for aliases and usage. The status bar shows background
jobs (`bg:N`), unread proactive alerts (`alerts:N`) and MCP connection counts. The
full list is in
[docs/slash-commands.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/slash-commands.md).

## Documentation

| Document | Contents |
| :--- | :--- |
| [docs/usage-guide.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/usage-guide.md) | Day-to-day use: tiers, memory, extensions, troubleshooting |
| [docs/slash-commands.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/slash-commands.md) | Every REPL command, alias and usage string |
| [docs/mcp.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/mcp.md) | MCP server and client, transports, trust gating |
| [docs/native-engine.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/native-engine.md) | The experimental native orchestration engine |
| [docs/lifecycle.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/lifecycle.md) | Update, repair, uninstall and purge-data |
| [docs/data-inventory.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/data-inventory.md) | What Velune stores and where |
| [docs/development.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/development.md) | Bootstrap and DI layer, module boundaries, testing, CI, extension points |
| [docs/project-origin.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/project-origin.md) | Release history and provenance |
| [CHANGELOG.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CHANGELOG.md) | Version history |
| [AUTHORS.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/AUTHORS.md), [GOVERNANCE.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/GOVERNANCE.md), [NOTICE](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/NOTICE) | Authorship, governance and licensing notices |

## Development setup

For working on Velune itself; to use Velune, follow the [Installation](#installation)
section instead.

```bash
git clone https://github.com/Surya-Hariharan/Velune-CLI.git
cd Velune-CLI
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

ruff check velune/
ruff format --check velune/
pyright velune/
pytest -q
```

`uv.lock` pins the resolved dependency graph; run `uv lock --check` after changing
dependencies in `pyproject.toml`. The full workflow, including pre-commit hooks,
is in [CONTRIBUTING.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CONTRIBUTING.md).

## Contributing

Contributions are welcome. For anything larger than a small fix, open an issue
first to discuss the scope. Read
[CONTRIBUTING.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CONTRIBUTING.md)
for setup, how to add a provider, command or council agent, and the pull request
checklist. Participation is governed by the
[Code of Conduct](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CODE_OF_CONDUCT.md).

## Security

Do not report vulnerabilities in public issues. Use a
[private GitHub security advisory](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new).
The policy, threat model, known limitations and response targets are in
[SECURITY.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/SECURITY.md).

## License and attribution

Velune CLI is licensed under the
[Apache License 2.0](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/LICENSE).
Copyright 2026 Surya HA.

Velune CLI was created and is maintained by Surya HA. This repository is the
canonical source; official releases are the tags and GitHub Releases published
here and the [`velune-cli`](https://pypi.org/project/velune-cli/) package on
PyPI. If you redistribute the software or a derivative, keep the copyright and
[NOTICE](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/NOTICE)
attributions as the license requires.

The license applies to the source code. Under Section 6 of the license it does
not grant permission to use the "Velune" and "Velune CLI" names, wordmark or logo
other than for reasonable and customary description of the origin of the work.
This is a statement about project naming, not a claim of registered trademark
status. Forks are welcome; they are separate projects and should not present
themselves as the canonical Velune CLI. Third-party product and company names
(for example Claude, Ollama, OpenAI) belong to their owners and are used only to
identify the services Velune can connect to. See
[AUTHORS.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/AUTHORS.md)
and
[GOVERNANCE.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/GOVERNANCE.md).
