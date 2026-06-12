# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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

## [1.1.0] — 2026-06-07

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

[Unreleased]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Surya-Hariharan/Velune-CLI/releases/tag/v0.1.0
