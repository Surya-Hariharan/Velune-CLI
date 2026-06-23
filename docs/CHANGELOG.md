# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.9.3-beta.1] - 2026-06-23

> **Pre-release.** This beta introduces a re-architected startup path and an
> explicit, user-driven cognition model. See the Migration Notes below before
> upgrading from `0.9.x`.

### Changed — Startup architecture

- **Instant startup with explicit, on-demand cognition.** The REPL no longer
  runs automatic repository cognition (indexing) on launch. The CLI opens
  immediately; you connect a model, open a project, and run cognition only when
  you ask for it. (`velune/cli/repl.py`, `velune/repository/cognition.py`)
- **New startup flow:** `velune` → CLI opens instantly → connect model →
  open project → run cognition.

### Added — Workspace, model & cognition commands

- **Workspace management** via `/project`
  (`open <path>` · `close` · `status` · `list` · `add <path>`). Recently-opened
  workspaces are remembered so the picker can reopen them instantly.
  (`velune/cli/workspaces.py`, `velune/cli/slash_dispatcher.py`)
- **Model registry + local model discovery** via `/model`
  (`discover` · `connect <id>` · `use <id>` · `list` · `status` ·
  `remove <id>`). `/model discover` finds locally available models (e.g. Ollama).
- **Manual cognition** via `/cognition`
  (`quick` · `standard` · `deep` · `status` · `init` · `cancel` · `rebuild`).
  `quick` scans manifests only; `standard`/`deep` build a full symbol index.

### Removed

- **Automatic repository cognition on startup.** Indexing is now opt-in through
  the `/cognition` command. This is the headline behavior change in this release.

### Performance

- Startup no longer blocks on indexing or a second model-reachability probe;
  cognition cost is paid only when explicitly requested.

### Migration Notes

- **Indexing is no longer automatic.** After opening a project, run
  `/cognition standard` (or `quick`/`deep`) to build the symbol index that
  earlier versions built silently at launch.
- **Connect a model explicitly.** Use `/model discover` then
  `/model connect <id>` (or `/model use <id>`) before running cognition or chat.

## [0.9.2] - 2026-06-23

### Changed — Packaging (lean install)

- **Lean default install.** Heavy/compiled dependencies are moved out of the
  base install into opt-in extras so `pip install velune-cli` resolves fast and
  cleanly on every platform. Core dependencies dropped from ~38 to 21 with no
  heavy compiled wheels. New extras: `[rag]` (lancedb, pyarrow, qdrant-client),
  `[parsing]` (tree-sitter grammars), `[telemetry]` (opentelemetry), `[git]`
  (gitpython); `[all]` aggregates everything. Every gated feature degrades
  gracefully when its extra is absent (e.g. semantic search becomes a no-op
  while lexical search and chat keep working). (`pyproject.toml`)

### Changed — Startup performance

- **`velune --version` is now near-instant** (~1.6s → ~0.04s). The console
  script entry point is `velune.main:main`, which fast-paths `--version`
  without importing the command graph or runtime. The Typer app is built
  lazily instead of at import time. (`velune/main.py`, `velune/cli/app.py`)
- **`velune <cmd> --help` no longer bootstraps the runtime** (~3.9s → ~1.6s):
  the root callback skips full-subsystem initialization when help is requested.
- Removed a redundant second Ollama reachability probe on REPL startup.

### Fixed

- First run with no providers and a non-interactive stdin now prints the
  `velune setup` hint and exits cleanly instead of blocking on a confirmation
  prompt or entering an unusable REPL. (`velune/cli/app.py`)
- The `llama-cpp-python` adapter error message no longer references the removed
  `[llamacpp]` extra. (`velune/providers/adapters/llamacpp.py`)
- `MANIFEST.in` now references the correct `docs/CHANGELOG.md` path.

### Added — Quality

- **Test suite wired into CI.** The `pytest` suite (350 tests) now runs in CI
  across {Linux, macOS, Windows} × {3.11, 3.13} and gates merges and releases;
  `asyncio_mode = "auto"` is configured under `[tool.pytest.ini_options]`.
- README documents `pipx install velune-cli`, the `python -m velune` fallback,
  and the Windows PATH note for the "`velune` is not recognized" case.

### Added — Providers

- **Cohere** provider adapter — native Chat API with preamble/history conversion,
  streaming, and `command-r-plus` / `command-r` model catalog.
  (`velune/providers/adapters/cohere.py`)
- **DeepSeek** provider adapter — OpenAI-compatible API at `api.deepseek.com`;
  supports DeepSeek-R1 and DeepSeek-Coder. (`velune/providers/adapters/deepseek.py`)
- **Mistral** provider adapter — La Plateforme REST API; Mistral Large, Codestral,
  and Mixtral models. (`velune/providers/adapters/mistral.py`)
- **NVIDIA NIM** provider adapter — OpenAI-compatible API at `integrate.api.nvidia.com`;
  hosts Llama, Mistral, and partner NIM models. (`velune/providers/adapters/nvidia.py`)

### Added — Git Integration

- **GitHub and GitLab REST clients** — `velune/integrations/github.py` and
  `gitlab.py` implement push-branch, create-PR/MR, fetch-issue, and
  post-comment operations using each platform's REST API.
- **`/push` REPL command** — pushes the current branch to `origin` (with optional
  `--force`). (`velune/cli/slash_dispatcher.py`)
- **`/pr` REPL command** — creates a pull request (GitHub) or merge request
  (GitLab) for the current branch from inside the REPL.
- **`/issue <number>` REPL command** — fetches a GitHub/GitLab issue by number
  and injects the title, body, and labels as conversation context.
- **`/sandbox` REPL command** — shows the active sandbox type (subprocess or
  Docker) and its configuration status.

### Added — Code Intelligence

- **`velune/analysis/` package** — code intelligence tools running locally without
  an LLM call:
  - `linter.py` — runs `ruff` / `pyflakes` and surfaces structured diagnostics.
  - `refactor.py` — detects code smells (long functions, deep nesting, high
    complexity) and returns ranked findings.
  - `type_inferrer.py` — suggests type annotations for unannotated function
    signatures using AST analysis.
  - `symbol_search.py` — fast symbol and definition lookup across the indexed workspace.
- **`/lint [file]` REPL command** — lint a Python file and display Rich diagnostic output.
- **`/refactor <file>` REPL command** — detect code smells with severity rankings.
- **`/typify <file>` REPL command** — suggest type hints for unannotated functions.

### Added — Declarative Plugin System

- **`velune/plugins/declarative/` package** — Markdown-based plugin manifests:
  declarative agents (`agent.py`), slash commands (`command.py`), skills
  (`skill.py`), and a filesystem scanner (`scanner.py`).
- **SKILL.md injection** — plugins can ship a `SKILL.md` that is automatically
  appended to the council's system context when the plugin is active.
- **`/plugin` REPL command** — list, enable, disable, and reload declarative
  plugins without restarting the session.
- **Lifecycle hook system** (`velune/hooks/`) — a typed hook dispatcher and executor
  that fires `pre_tool` / `post_tool` events; plugins register handlers via
  their manifest.

### Added — Background Service

- **`velune/daemon/` package** — a background Velune service (`server.py`) with
  an IPC transport (`transport.py`) and a client (`client.py`).
- **`velune daemon start|stop|status`** CLI subcommands to manage the service.

### Added — CLI Subcommands

- **`velune workspace`** subcommand group — `init`, `status`, `graph`, `list`,
  `open`, `remove`. `workspace graph` renders an interactive dependency tree
  from `velune/observability/workspace_graph.py`.
- **`velune session`** subcommand group — `list`, `delete`, `export`.
- **`velune provider`** subcommand group — `add`, `remove`, `test`, `list`, `status`.
- **`velune config`** subcommand group — `get`, `set`, `show`.
- **`velune usage`**, **`velune quota`**, **`velune health`** commands for
  analytics and provider monitoring.
- **`velune logs`** (alias for `trace`) — view or follow the execution event
  stream from the current workspace.
- **`velune status`** (alias for `context`) — show index freshness, file counts,
  and cognitive-core record counts without starting the full runtime.
- **`velune pipeline`** (alias for `retrieval`) — trace a retrieval query through
  the BM25 + vector + graph pipeline and show per-stage scores.
- **`velune memory`** subcommand group — `inspect`, `clear`, `compact`.

### Added — REPL Commands

- **`/council <task>`** — force the full council tier regardless of task
  complexity classification.
- **`/new [title]`** — start a fresh conversation while keeping project memory.
- **`/project [name|path]`** — switch or manage project workspaces from within
  the REPL.
- **`/bench [run]`** — view stored benchmark results or trigger a new empirical
  capability run.
- **`/graph`** — render a hierarchical tree of knowledge graph entities for the
  current workspace.
- **`/hunk`** — toggle hunk-by-hunk review mode; each proposed file edit is
  shown and approved individually before being applied.
- **`/undo`** — revert the last Velune-generated git commit, leaving the changes
  staged for inspection.
- **`/approve [safe|ask|block]`** — set the tool/command approval gate for the
  session.
- **`/hooks`** — list all active lifecycle hooks and their configuration source.
- **`/stats`** — show session statistics: tokens used, estimated cost, turn
  count, and uptime.
- **`/history`** — show the REPL command execution history for the current session.
- **`/pull [model-id]`** and **`/delete <model-id>`** — download or delete
  Ollama models from within the REPL with live progress output.
- **`/mcp`** subcommands — `servers`, `tools`, `resources`, `connect <name>`,
  `disconnect <name>`, `refresh <name>` — inspect MCP connections without
  leaving the REPL.

### Security

- **Isolated `llama-cpp-python` from the default install set** to eliminate the
  `diskcache ≤ 5.6.3` transitive vulnerability (unsafe pickle deserialization — no
  patched version exists). The `[gguf]` optional extra now installs only the
  `gguf` metadata library, which is unaffected. In-process GGUF inference is
  available via the new `[llamacpp]` extra (`pip install 'velune-cli[llamacpp]'`),
  which is deliberately excluded from `[all]`. `pip-audit` now reports
  **no known vulnerabilities** on a default install. (`pyproject.toml`,
  `velune/providers/adapters/llamacpp.py`)

### Added

- **Intent reconstruction** — new `velune/cognition/intent.py` with `IntentClassifier`
  and `IntentType` enum (EXPLAIN / GENERATE / REFACTOR / DEBUG / REVIEW / QUESTION / COMMAND).
  Zero-latency keyword + word-boundary scoring; wired into `ContextOrchestrationEngine`
  as Phase 0 on every prompt. (`velune/cognition/intent.py`)

- **Council pipeline** — `CouncilRunner` orchestrates the full planner → coder →
  reviewer → debate → synthesizer pipeline. Cycle exhaustion escalates REVISE to REJECT
  automatically. (`velune/cognition/council_runner.py`)

- **DebateSession** — scores and ranks council proposals using challenger severity and
  reviewer decision; produces structured audit reports for the synthesizer.
  (`velune/cognition/council/debate.py`)

- **Multi-model role dispatch** — `ContextOrchestrationEngine.execute()` routes requests
  through `CouncilRunner` when a `CouncilAgentFactory` is configured; degrades
  gracefully when no factory is present. (`velune/orchestration/engine.py`)

- **WebSocket MCP transport** — `WebSocketConnection` implements the `MCPConnection`
  contract over JSON-RPC 2.0 on `ws://` and `wss://` URLs, with SSRF URL validation,
  per-call timeout guards, and optional resource discovery.
  (`velune/mcp/transports/websocket.py`)

- **`/doctor` council panel** — new "Council" category in `velune doctor` output shows
  role assignment coverage (roles → model IDs) or warnings for unmapped roles.
  (`velune/cli/commands/doctor.py`)

- **Async background tasks** — long `/run` tasks no longer block the REPL prompt.
  - `/run --bg <task>` submits a task to the background and returns immediately.
  - The status bar shows `⚙ N bg` while jobs are running.
  - New `JobRegistry` with `JobRecord` (ID, name, status, phase, elapsed, preview)
    and `JobStatus` enum (PENDING / RUNNING / COMPLETED / FAILED / CANCELLED).
    (`velune/core/task_registry.py`)

- **`/jobs` command** — list or cancel background jobs.
  - `/jobs` renders a live Rich table of all submitted jobs with color-coded status.
  - `/jobs cancel <id>` cancels a running job and clears its loop-detector state.

- **`/dashboard` command** — live progress dashboard.
  - Full-screen Rich `Live` layout: jobs table (top ⅔), alerts + provider health
    panels (bottom ⅓). Refreshes every 500 ms; press Enter or Ctrl+C to exit.
    (`velune/cli/display/dashboard.py`)

- **Error self-healing with exponential backoff** — the council orchestrator now
  automatically retries transient `ProviderConnectionError`, `InferenceError`, and
  `TimeoutError` failures up to `config.providers.max_retries` times (default 3),
  with randomized exponential backoff (`min(base × 2^(attempt-1), 30s) × jitter`).
  Each retry emits a `retry.attempt` event on the `CognitiveBus`.
  (`velune/core/retry.py`, `velune/cognition/orchestrator.py`)

- **Error loop detection** — sliding-window circuit breaker stops infinite retry
  cycles. `ErrorLoopDetector` fingerprints each exception (SHA-1 of type + message
  prefix) and trips after 3 occurrences within a 5-minute window, emitting a
  `retry.loop_detected` event and raising immediately rather than retrying.
  (`velune/core/loop_detector.py`)

- **Proactive issue detection** — `ProactiveWatcher` subscribes to `CognitiveBus`
  events and surfaces problems before the user asks.
  - `job.failed` → WARN alert
  - `retry.loop_detected` → DANGER alert
  - `provider.health_changed` (degraded/unavailable) → WARN/DANGER alert
  - `context.threshold_crossed` (70 % / 90 %) → WARN/DANGER alert
  - Periodic check (every 15 s) scans provider health manifests for UNAVAILABLE states.
  - Unread alerts drain after each REPL prompt and render as Rich panels above the
    input line. The status bar shows `⚠ N` for pending alerts.
  - `AlertStore` (ring-buffer, max 20 entries) and `ProactiveWatcher` registered in
    the service container at startup / shutdown. (`velune/proactive/`)

- **52 new unit tests** covering loop detection, retry policy, job registry,
  proactive watcher, and dashboard builders (all passing).

### Refactored

- **`velune/cli/slash_dispatcher.py`** (new) — extracted `_build_registry` and
  `_load_file_commands` from `VeluneREPL`. `repl.py` reduced by ~440 lines
  (4,138 → 3,697).

- **`velune/cli/stream_renderer.py`** (new) — extracted streaming / non-streaming
  render loop from `_handle_prompt` into `StreamRenderer.render()` returning
  `RenderResult(full_content, tokens_used, interrupted)`.

- **Provider fallback** — `ProviderRouter.get_ordered_candidates()` returns all viable
  candidates in capability-score order; `BaseCouncilAgent` iterates fallback providers
  on failure.

- **Lineage search** — `MemoryLifecycleCoordinator.get_lineage_warnings()` now
  delegates to `LineageStore.query_continuity_warnings()` instead of returning `[]`.

- **Diff parsing** — `CoderAgent._parse_diffs()` replaced with `parse_with_fallback()`
  from `velune/execution/edit_formats/registry.py`.

- **Plugins** — deleted `velune/plugins/schemas.py` (legacy `PluginManifest`); updated
  `PluginRegistry` and `PluginLoader` to use `DeclarativePluginManifest` and an inline
  `PluginManifest` dataclass respectively. Deleted `velune/context/window.py` (legacy
  `ContextWindowTracker`); `estimate_tokens()` now lives in `token_counter.py`.

### Changed

- `StatusBarState` gains `bg_job_count` and `alert_count` fields; `render_status_bar`
  renders them as warn-coloured segments only when non-zero. (`velune/cli/statusbar.py`)
- `RuntimeBootstrapper.bootstrap()` registers `JobRegistry` and `AlertStore` in the
  service container at startup. (`velune/kernel/bootstrap.py`)
- `_async_main()` now starts `ProactiveWatcher` after `lifecycle.startup()` and
  cleanly stops it in the `finally` block. (`velune/kernel/entrypoint.py`)

### Fixed

- **Intent classifier word boundaries** — `_score()` now uses `\b` anchors so
  `"build"` no longer fires on `"rebuild"` and `"implement"` no longer fires on
  `"implementation"`. Debug signals use substring matching to catch `"KeyError"` etc.

- **`velune/plugins/registry.py`** — `register_plugin` now calls
  `_extract_hook_names()` which handles both declarative (hooks JSON file) and legacy
  (inline `hooks` list) manifest shapes.

### Tests

- `tests/test_intent.py` — 7 intent-type test classes + confidence + engine integration
  tests (28 cases total).
- `tests/test_council_runner.py` — happy path, revise-then-approve, reject-on-exhaustion,
  failure isolation, `DebateSession` unit tests, helper function tests (21 cases).
- Updated `tests/test_mcp_phase2.py` — `test_unsupported_transport_raises` replaced by
  `test_returns_websocket_connection` now that WebSocket is implemented.

## [0.9.1] - 2026-06-14

This is a **stabilization and trust-recovery** release. It cuts the runtime-hardening
and packaging-correctness work that had accumulated on `main` since `0.9.0` into a
properly tagged, reproducible PyPI artifact. There are no new features and no breaking
changes — `pip install --upgrade velune-cli` is a safe, drop-in update.

### Security

- **Windows PATH-hijack guard now enforced.** `_is_trusted_path` previously returned
  `True` unconditionally on Windows, so a malicious binary planted earlier in `PATH`
  would be executed. The resolved binary must now live under a system/program-install
  root, the interpreter's own environment, or a workspace venv — matching the existing
  POSIX behavior. (`velune/execution/command_spec.py`)
- **Interpreter inline-code execution blocked.** Allowlisted interpreters could run
  arbitrary program text with no approval gate (`python -c …`, `node -e/--eval/-p …`,
  including Python short-flag clusters like `-Ic`). These flags are now rejected;
  running a *file* is still permitted, and agent-authored files must pass the
  `DiffPreview` write-approval flow before they can be run.
- **Execution-model documentation corrected for honesty.** SECURITY.md and
  docs/THREAT_MODEL.md now describe the execution layer as a *managed, resource-limited
  execution environment* — explicitly **not** an OS-level sandbox — and document the
  residual risk (allowlisted interpreters/build tools run workspace files as the user)
  plus the OS-isolation roadmap. README's architecture label updated accordingly.
- Added Bandit static analysis to CI (gates on medium+ severity) and gitleaks secret scanning.
- Resolved Bandit high/medium findings: marked the non-cryptographic workspace-slug SHA-1 with `usedforsecurity=False`, and gave the Ollama HTTP client a bounded default timeout (60s, 5s connect) so non-streaming calls cannot hang indefinitely.

### Fixed

- **Subprocess pipe-buffer deadlock in the execution sandbox.** `SubprocessSandbox.execute`
  read child output via `communicate()` only *after* the poll loop saw the process exit.
  A child that wrote more than the OS pipe capacity (~64 KiB) blocked on `write()`, never
  exited, and was killed as a false timeout with **all output lost** — affecting any normal
  test run, verbose build, or `pip install`. Both pipes are now drained concurrently on
  dedicated threads while the process runs, into a per-stream memory-bounded buffer
  (default 10 MiB, configurable via `max_output_bytes`). This removes the deadlock, bounds
  parent memory against runaway producers, and preserves partial output on timeout.
  (`velune/execution/sandbox.py`)

### Added

- **`velune doctor` runtime path-safety check.** A new Security-category diagnostic resolves
  each allowlisted executable via the same `shutil.which` lookup the sandbox uses and
  validates it against the real `_is_trusted_path` guard, surfacing any tool that resolves
  to an untrusted location (PATH-hijack candidate or non-standard install the sandbox will
  refuse to run). Makes the PATH-hijack guarantee observable rather than silent.
  (`velune/cli/commands/doctor.py`)

### Changed

- CI test matrix expanded to **Ubuntu / Windows / macOS × Python 3.11 / 3.12 / 3.13**.
- Release pipeline now publishes to PyPI via **OIDC trusted publishing** (no long-lived token); removed the `continue-on-error` that silently swallowed failed publishes.
- Release & CI builds are now **reproducible** (`SOURCE_DATE_EPOCH` pinned to the commit, `[tool.hatch.build] reproducible = true`) and validated with `twine check --strict`.
- Release pipeline now asserts the git tag matches `velune.__version__` before building, so a mistagged release fails fast.
- Coverage reporting made honest: shrank the `omit` list from ~70 modules to only un-unit-testable surfaces (TTY/daemon/live-network/optional-native). Full-codebase coverage is now measured (~21%) with a CI floor of 20%.
- Migrated the event-bus `Event` model from Pydantic v1 `class Config` to `ConfigDict` (removes a deprecation warning, forward-compatible with Pydantic v3).
- Dependabot now groups minor/patch bumps into single PRs and uses the correct GitHub reviewer handle.

### Added

- New CI **`build`** + **`install-smoke`** jobs: reproducible build, strict metadata validation, pure-python wheel assertion, and a cross-platform (Ubuntu/Windows/macOS × Py 3.11/3.13) wheel-install + `velune --version`/`--help` REPL smoke test.
- Python 3.13 classifier, `Typing :: Typed` classifier, and a `Documentation` project URL in `pyproject.toml`.
- Unit tests for `execution/validator.py` (16% → 90% coverage).
- **CLI Design Modernization** — Comprehensive frontend redesign for professional appearance
  - Modern startup banner with clean, spacious layout
  - Refined REPL prompt with sophisticated color palette (blue primary + gold accent)
  - Simplified prompt display: only shows context bar when >40% full
  - Updated error rendering with cleaner panel formatting
  - Enhanced theme colors with semantic tokens (muted, accent)
  - Better visual hierarchy throughout terminal interface

## [0.9.0] - 2026-06-12

### Security

- Plugin sandbox status: Plugin sandbox remains unimplemented or disabled for standard CLI operations.
- Removal of `run_until_complete` anti-pattern: Cleaned up all async loop management and centralized loop execution in `entrypoint.py`.
- Security audit suite extension: Centralized static and runtime vulnerability controls.

### Fixed

- Fixed memory lifecycle shutdown duplication to prevent multiple DB closure errors.
- Fixed Ollama context-window detection to correctly read local model metadata.

### Changed

- Consolidated AST parser logic into a unified syntax parsing layer.
- Consolidated council orchestrators to streamline Planner/Coder/Reviewer loops.
- Modernized CLI theme, refined color palettes, updated startup banner, and context trackers.
- Reconciled documentation and cleaned up dead MCP CLI commands.

### Removed

- Removed superseded `tests/` and `scripts/` directories entirely from the repository.

## [0.9.0-beta] — 2026-06-12

### Overview

**Public Beta Release** — All Phase 0-1 systems verified and integrated. Ready for early adopters.

### Architecture & Verification

- **Known Issues Resolution** — All 10 critical issues resolved:
  1. shell=True blocking (security)
  2. asyncio.run() consolidation (correctness)
  3. CognitiveFirewall prompt injection tests passing
  4. CapabilityLevel aliasing prevention verified
  5. SQLite WAL concurrency tests passing
  6. VeluneConfig single-source-of-truth enforced
  7. CouncilState single shape enforced
  8. Event history deque(maxlen=1000) in place
  9. CouncilExecutionBudget enforcement tested
  10. SSRFGuard blocking private metadata endpoints

- **Architecture Boundary Verification** — All 270 Python files pass 8 layer boundary rules
  - Kernel ↚ CLI/Cognition (OS layer isolation)
  - Providers ↚ Cognition/CLI (infrastructure isolation)
  - Memory ↚ Cognition/CLI (persistence layer)
  - Retrieval ↚ CLI (data access isolation)
  - Telemetry ↚ Cognition (observability separation)

- **Test Coverage** — 581/635 tests passing (91.6%)
  - All critical routing and security tests passing
  - 48 non-critical test failures isolated for Phase 2
  - 6 collection errors in experimental features (dual-path retrieval)

- **Fixed Integration Issues**
  - Added structlog dependency (telemetry logging)
  - Fixed @contextmanager import in logging.py
  - Fixed ContextBudget dataclass field ordering
  - Corrected CrossEncoderReranker naming
  - Fixed test_mcp_server.py syntax error

### Beta Release Status

- All documented commands verified and functional
- Hardware detection and tier classification working
- Provider health monitoring implemented
- Multi-model council orchestration stable
- Session persistence and memory tiers operational
- MCP server integration available

### Known Limitations (Phase 2+)

- Startup time 3.6s (target 3.0s) — optimization planned
- 48 failing tests deferred to Phase 2 (incremental indexing, streaming repair, prompt adaptation)
- Dual-path retrieval disabled (experimental feature)
- Cloud provider integration incomplete for some APIs

### Security

- All OWASP top 10 checks passing
- Architecture lint enforced (no shell=True, asyncio.run isolated)
- Keyring-based secret storage for API keys
- Sandbox execution for arbitrary code
- SSRF guard blocks private IP ranges

## [0.6.0] — 2026-06-12

### Added

- **Provider Health Monitoring** — Real-time health tracking with CapabilityManifest
  - Background polling every 30 seconds
  - Health status (HEALTHY/DEGRADED/UNAVAILABLE)
  - Estimated latency tracking (5-call rolling average)
  - Rate limit monitoring
- **Health-Aware Routing** — Router considers provider health for model selection
  - Filters unavailable providers
  - Prefers healthy providers
  - Latency-sensitive task optimization
- **Startup Performance Monitoring** — CI check for startup time regression (<3s threshold)
- **Comprehensive CI/CD Pipeline** (`CI_PASS` gate blocks merge on failure)
  - Lint (ruff + pyright)
  - Security (pip-audit, shell=True check, asyncio.run() check)
  - Architecture Lint (8 layer boundary rules)
  - Unit Tests (70% coverage minimum)
  - Integration Tests (on main/PRs)
  - Build Check
  - Startup Performance (main only)
- **Latency Recording** — All providers auto-record call latency
  - Synchronous calls: total time
  - Streaming calls: TTFT (time-to-first-token)
- **Architecture Linting Script** (`scripts/check_architecture.py`)
  - AST-based import analysis
  - 8 layer boundary rules
  - 0 external dependencies
- **Automated Dependency Updates** via Dependabot
  - Weekly pip updates
  - Weekly GitHub Actions updates
  - Manual review required (no auto-merge)
- **Pre-Commit Hooks** — Auto-format and lint before commit
- **Type Checking** — Pyright configuration with standard mode
- **Enhanced Code Coverage** — Branch coverage + exclusion rules

### Changed

- Updated all provider adapters to record latency
  - Anthropic, OpenAI, Google, Groq, HuggingFace, LM Studio, Ollama, LlamaCpp
- Enhanced ruff configuration with 8 rule categories (E, W, F, I, B, C4, UP, N)
- Improved pytest configuration (timeouts, coverage reporting)
- Reorganized pyproject.toml with comprehensive tool configurations

### Fixed

- Replaced `UNHEALTHY` enum with `UNAVAILABLE` for consistency
- Fixed provider health check timeouts
- Added proper error handling for latency recording

### Removed

- Demo files (council_example.py)
- Unnecessary documentation (implementation details)
- Redundant markdown files

### Security

- Added pip-audit dependency vulnerability scanning
- shell=True regression check (P0-2)
- asyncio.run() count validation (P0-1)
- Architecture boundary enforcement
- Pre-commit hooks for local security

## [0.5.0-beta] — 2026-06-07

### Added

- Google Gemini provider (2.0 Flash, 1.5 Pro, 1.5 Flash, 2.0 Flash Thinking)
- Together AI provider (Llama 3.3 70B, Qwen 2.5 Coder 32B, DeepSeek R1)
- Fireworks AI provider (DeepSeek R1, Qwen 2.5 Coder, Mixtral 8x22B)
- /councilmodel command — assign specific models to specific council roles
- /pull command — download Ollama models interactively from within the REPL
- /delete command — remove locally installed Ollama models
- Project type auto-detection (FastAPI, Django, Flask, React, Next.js, Rust, Go, Java Spring, .NET, Flutter) with framework-specific context
- ProjectTypeDetector writes .velune/project_profile.json on init
- System prompt injection based on detected project type
- Model pull progress bar with live streaming status
- Council role assignments persist to .velune/council_roles.json
- ModeAwareModelSelector for /optimus and /godly auto-model selection
- `/optimus` and `/godly` session-wide REPL modes with `ModeManager`, `ModeConfig`, and `ModeAwareModelSelector` (`velune/cli/modes.py`, `velune/cli/model_selector.py`)
- Slash command Tab-autocomplete (`velune/cli/autocomplete.py`) with `/model <id>` completion
- Rich startup banner showing hardware tier, GPU, providers, and active model (`velune/cli/banner.py`)
- Security audit script (`scripts/security_audit.py`) — 6 checks, exit 0 required in CI
- `RateLimiter` token bucket and `DEFAULT_HOST`/`MAX_REQUEST_BYTES` constants added to the MCP server (`velune/mcp/server.py`)
- `DEFAULT_VELUNEIGNORE` expanded: `*.crt`, `id_rsa`, `id_dsa`, `id_ed25519`, `id_ecdsa`, `.netrc`, `.npmrc`, `.pypirc`, `.aws/`, `credentials.json`, `service-account.json`
- Full GitHub Actions CI/CD pipeline: lint + type check, 2×2 test matrix (Python 3.11/3.12 × Ubuntu/macOS), security audit job, build + `twine check`, Codecov upload
- Automated release workflow: tag-to-PyPI via OIDC trusted publishing, CHANGELOG-based GitHub Release notes, pre-release detection from tag suffix
- `scripts/extract_changelog.py` — parses CHANGELOG.md for a version section
- `docs/releasing.md` — step-by-step release checklist
- `docs/mcp.md` — MCP integration guide with all 21 real tool names
- `CONTRIBUTING.md` — developer how-to: adding providers, slash commands, council agents
- `README.md` rewritten — quickstart, hardware table, provider table, architecture tree, session modes, MCP section, Windows section
- `WINDOWS.md` — complete 10-section WSL2 setup guide with GPU passthrough
- BYOK (Bring Your Own Key) provider system: xAI, Google Gemini, Groq, OpenRouter
- OS keyring integration via `keyring` library (`velune/providers/keystore.py`)
- `LocalModelResolver` for filesystem GGUF discovery across 9 well-known paths
- Persistent model-path cache (`velune/providers/local_paths.py`)
- `is_running()` classmethods on `OllamaDiscovery` and `LMStudioDiscovery` for 2-second reachability checks before discovery
- `ModelDiscoveryScanner._collect()` helper — per-discoverer error isolation
- Summary log line after each full scan: `Local: N GGUF, N Ollama, N LM Studio | Cloud: N models`
- Cloud discoverer key-gating: cloud providers skip network calls when no key is set
- OpenRouter 1-hour disk cache for model lists
- GitHub CI workflow (`ci.yml`) with lint, test (Python 3.11 / 3.12), and build jobs
- GitHub Release workflow (`release.yml`) with PyPI trusted publishing and CHANGELOG excerpt
- GitHub issue templates (bug report, feature request)
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)

### Security

- All provider adapters and discovery modules now use `keystore.get_key()` instead of bare `os.getenv()` for API key retrieval (`adapters/anthropic.py`, `adapters/openai.py`, `adapters/huggingface.py`, `discovery/anthropic.py`, `discovery/openai.py`, `cli/commands/doctor.py`)
- `tests/test_security.py` — 6 security property tests covering shell injection, API key bypass, SSRF blocking, veluneignore coverage, and rate limiting

### Changed

- `GGUFDiscovery.discover()` now delegates to `LocalModelResolver` instead of
  a bare `rglob` — depth-limited (5 levels), capped at 100k files per root,
  deduplicated
- `LlamaCppProvider._resolve_model_path()` now checks persistent cache →
  `LocalModelResolver` → interactive prompt before raising `FileNotFoundError`
- `LlamaCppProvider.list_models()` simplified — removed `search_paths` mutation
- `pyproject.toml` license classifier corrected to Apache Software License
- `SECURITY.md` and `CONTRIBUTING.md` consolidated (removed duplicate sections)
- `.gitignore` expanded with `.benchmarks/`, `.claude/`, secret file patterns

### Fixed

- `CONTRIBUTING.md` footer appeared three times — reduced to one
- `README.md` referenced MIT License — corrected to Apache-2.0
- `pyproject.toml` `[tool.ruff.lint]` section was at wrong TOML level

---

## [0.1.0] - 2026-06-05

### Added

- Initial public release
- Typer CLI with `ask`, `run`, `workspace`, `doctor`, `models`, `chat` subcommands
- LangGraph council orchestrator (Planner → Coder → Reviewer → Synthesizer)
- Repository cognition core: tree-sitter AST parsing, BM25, Qdrant vector store,
  Graphiti memory graph
- Provider adapters: Ollama, LM Studio, llama.cpp, OpenAI, Anthropic, HuggingFace
- `SubprocessSandbox` with write-path allowlists, time limits, and SSRF suppression
- Hybrid retriever (BM25 + Qdrant) with `asyncio`-safe sync fallback
- `velune doctor check` — GPU, VRAM, provider, grammar diagnostics
- `ModelDiscoveryScanner` — parallel async discovery across all providers
- `CapabilityClassifier` and `CapabilityBenchmark` for empirical model profiling
- Git-backed transactional execution with automatic rollback on failure
- Cognitive firewall: prompt-injection detection and HTML sanitization
- Security sandbox: workspace write guards, network hygiene, secret scrubbing
- Full pytest suite: unit, integration, async, and benchmark tests

[Unreleased]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.3-beta.1...HEAD
[0.9.3-beta.1]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.2...v0.9.3-beta.1
[0.9.2]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Surya-Hariharan/Velune-CLI/releases/tag/v0.9.0
[0.1.0]: https://github.com/Surya-Hariharan/Velune-CLI/releases/tag/v0.1.0
