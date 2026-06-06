# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- `/optimus` and `/godly` session-wide REPL modes with `ModeManager`, `ModeConfig`,
  and `ModeAwareModelSelector` (`velune/cli/modes.py`, `velune/cli/model_selector.py`)
- Slash command Tab-autocomplete (`velune/cli/autocomplete.py`) with `/model <id>` completion
- Rich startup banner showing hardware tier, GPU, providers, and active model
  (`velune/cli/banner.py`)
- Security audit script (`scripts/security_audit.py`) — 6 checks, exit 0 required in CI
- `RateLimiter` token bucket and `DEFAULT_HOST`/`MAX_REQUEST_BYTES` constants added to
  the MCP server (`velune/mcp/server.py`)
- `DEFAULT_VELUNEIGNORE` expanded: `*.crt`, `id_rsa`, `id_dsa`, `id_ed25519`, `id_ecdsa`,
  `.netrc`, `.npmrc`, `.pypirc`, `.aws/`, `credentials.json`, `service-account.json`
- Full GitHub Actions CI/CD pipeline: lint + type check, 2×2 test matrix (Python 3.11/3.12
  × Ubuntu/macOS), security audit job, build + `twine check`, Codecov upload
- Automated release workflow: tag-to-PyPI via OIDC trusted publishing, CHANGELOG-based
  GitHub Release notes, pre-release detection from tag suffix
- `scripts/extract_changelog.py` — parses CHANGELOG.md for a version section
- `docs/releasing.md` — step-by-step release checklist
- `docs/mcp.md` — MCP integration guide with all 21 real tool names
- `CONTRIBUTING.md` — developer how-to: adding providers, slash commands, council agents
- `README.md` rewritten — quickstart, hardware table, provider table, architecture tree,
  session modes, MCP section, Windows section
- `WINDOWS.md` — complete 10-section WSL2 setup guide with GPU passthrough

### Security

- All provider adapters and discovery modules now use `keystore.get_key()` instead of
  bare `os.getenv()` for API key retrieval
  (`adapters/anthropic.py`, `adapters/openai.py`, `adapters/huggingface.py`,
  `discovery/anthropic.py`, `discovery/openai.py`, `cli/commands/doctor.py`)
- `tests/test_security.py` — 6 security property tests covering shell injection,
  API key bypass, SSRF blocking, veluneignore coverage, and rate limiting

- BYOK (Bring Your Own Key) provider system: xAI, Google Gemini, Groq, OpenRouter
- OS keyring integration via `keyring` library (`velune/providers/keystore.py`)
- `LocalModelResolver` for filesystem GGUF discovery across 9 well-known paths
- Persistent model-path cache (`velune/providers/local_paths.py`)
- `is_running()` classmethods on `OllamaDiscovery` and `LMStudioDiscovery` for
  2-second reachability checks before discovery
- `ModelDiscoveryScanner._collect()` helper — per-discoverer error isolation
- Summary log line after each full scan: `Local: N GGUF, N Ollama, N LM Studio | Cloud: N models`
- Cloud discoverer key-gating: cloud providers skip network calls when no key is set
- OpenRouter 1-hour disk cache for model lists
- GitHub CI workflow (`ci.yml`) with lint, test (Python 3.11 / 3.12), and build jobs
- GitHub Release workflow (`release.yml`) with PyPI trusted publishing and CHANGELOG excerpt
- GitHub issue templates (bug report, feature request)
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)

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
