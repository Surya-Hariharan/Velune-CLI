# Architecture & Subsystem Status

This document describes the maturity levels, architectural roles, and known limitations of the primary subsystems in the Velune codebase.

## Subsystem Maturity Matrix

| Subsystem | Folder | Status | Maturity Notes |
| :--- | :--- | :--- | :--- |
| **CLI & REPL** | `velune/cli/` | **Stable** | Production terminal UI, autocomplete, slash commands, and color rendering. |
| **Cognition** | `velune/cognition/` | **Stable** | Planner, Coder, Reviewer, Challenger, Synthesizer, and Critics. Bounded & Full orchestrators. |
| **Providers** | `velune/providers/` | **Stable** | Keyring credential storage, health monitoring, and health-aware latency routing. |
| **Memory** | `velune/memory/` | **Stable** | SQLite episodic/lineage, LanceDB semantic vector storage, and graph relations. |
| **Retrieval** | `velune/retrieval/` | **Stable** | AST parsing, hybrid search, ranking, and context chunking. |
| **Execution** | `velune/execution/` | **Stable** | `SubprocessSandbox` and `PathGuard` workspace containment. |
| **Telemetry** | `velune/telemetry/` | **Stable** | Cost estimators, trace sink logging, and token usage estimators. |
| **Plugins** | `velune/plugins/` | **Experimental** | Loader and hook registry. Disabled by default; runs in-process without sandboxing. |

---

## Detailed Subsystem Status

### 1. CLI / UX Layer
- **Status**: Stable.
- **Details**: Built using `prompt-toolkit` and `rich`. Exposes full interactive REPL with fuzzy auto-completion for slash commands and model IDs. Includes session managers, picker UIs, and status indicators.

### 2. Cognitive Orchestration
- **Status**: Stable.
- **Details**: Offers two orchestration paths:
  1. **Bounded (3-agent)**: Planner → Coder → Reviewer. Lightweight, fast, budget-enforced execution.
  2. **Full (Multi-agent)**: Includes Challenger, Synthesizer, and specialized Critics (Scalability, Security, Performance, Maintainability). Leverages historical experiment logs (Lineage memory) to avoid repeating failed approaches.
- **Security**: The `CognitiveFirewall` filters inputs and quarantines/neutralizes suspected prompt injections before forwarding to LLMs, and wraps workspace code in untrusted boundaries.

### 3. Memory & Retrieval
- **Status**: Stable.
- **Details**: Implements five distinct memory tiers:
  - **Working**: Session conversation history.
  - **Episodic**: Local SQLite database storing conversation history across restarts.
  - **Semantic**: Multi-dimensional vector storage implemented via **LanceDB** (local, fast filesystem DB).
  - **Graph**: Workspace relationship graph.
  - **Lineage**: Logs decisions, architectural trade-offs, and prior failed experiments.

### 4. Providers & Keyring
- **Status**: Stable.
- **Details**: Support for local (Ollama) and cloud providers (Groq, OpenAI, Anthropic, xAI, Google Gemini, OpenRouter). Keys are stored in the secure OS keyring (no plain-text configuration files). A background monitor tracks latency (5-call rolling average) and availability, feeding health data to the provider router.

### 5. Execution Sandbox
- **Status**: Stable.
- **Details**: Handles execution of agent code inside a resource-limited sandbox. Command arguments are sanitized, and shell features/operators are rejected. Path traversal and symlink escapes are blocked via `PathGuard` verification. Native execution is supported on both **POSIX** (Linux/macOS) and **Windows** (win32).

---

## Known Limitations & Roadmap

### 1. No OS-level Sandbox Isolation
- **Current State**: The `SubprocessSandbox` enforces a strict binary allowlist, blocks interpreter inline-code flags (`python -c`, `node -e`), and bounds runtime memory/CPU. However, there is no namespace, container, or seccomp-level isolation. Allowlisted interpreters and build tools (`python`, `node`, `go`, `make`) can execute *existing files in the workspace* with the privileges of the invoking user.
- **Mitigation**: All file mutations (creates/writes/deletes) must pass the user's manual approval through the terminal `DiffPreview` flow before execution.
- **Roadmap**: Porting OS-level isolation (e.g. Linux `bubblewrap`/`firejail`, macOS `sandbox-exec`, Windows restricted tokens) is scheduled for future milestones.

### 2. Plugin In-Process Execution
- **Current State**: Plugins are loaded dynamically into the main Velune process. A malicious or buggy plugin has full access to the process environment, including OS credentials and files.
- **Mitigation**: The plugin subsystem is completely disabled by default and is unreachable via standard CLI commands. Loading requires an explicit experimental environment flag (`VELUNE_ENABLE_EXPERIMENTAL_PLUGINS=1`).
- **Roadmap**: Refactoring the plugin manager to launch plugins in isolated subprocesses.

### 3. Untrusted MCP Tool Outputs
- **Current State**: External Model Context Protocol (MCP) server responses are integrated into the context but are not yet scanned for prompt injection attacks.
- **Mitigation**: Keep connected MCP hosts restricted via the `allowed_hosts` config list.
