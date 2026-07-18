# Development Guide

*The internal engineering reference: how the codebase is put together at the
framework level, how CI validates it, and how to reason about changes that
cross subsystem boundaries.*

For "how do I set up a venv and open a PR," see
[CONTRIBUTING.md](../CONTRIBUTING.md) — that doc owns the step-by-step
contributor workflow; this one owns the *why* behind the structure so those
steps make sense.

---

## Contents

- [1. The bootstrap / DI layer](#1-the-bootstrap--di-layer)
- [2. Module boundaries — what depends on what](#2-module-boundaries--what-depends-on-what)
- [3. Testing philosophy](#3-testing-philosophy)
- [4. CI pipeline](#4-ci-pipeline)
- [5. Extension points — the design pattern behind all of them](#5-extension-points--the-design-pattern-behind-all-of-them)
- [6. Debugging tips specific to this codebase](#6-debugging-tips-specific-to-this-codebase)
- [7. Where things live, cross-referenced](#7-where-things-live-cross-referenced)

---

## 1. The bootstrap / DI layer

Everything in Velune CLI beyond the Typer command dispatch runs on top of a
small dependency-injection kernel in `velune/kernel/`:

- **`ServiceContainer`** (`kernel/registry.py`) — a registry of named
  services with lazy construction: a service is only built the first time
  something asks for it, and the same instance is reused after that.
  `get_container()` returns the process-wide singleton; `inject(name)` is
  the decorator/lookup helper used throughout the codebase instead of
  constructing subsystems directly.
- **`RuntimeEnvironment`** (`kernel/bootstrap.py`) — the object every
  subsystem module receives to register its services against. It carries
  workspace path, config, the container, and the lifecycle coordinator.
- **`SubsystemModule`** (`kernel/bootstrap.py`) — the unit of registration.
  Each major package (`memory`, `retrieval`, `cognition`, `mcp`, `resources`,
  `hooks`, `plugins`, …) exposes a `module.py` with factory functions that
  `RuntimeBootstrapper` wires into the container in dependency order. Each
  `SubsystemModule` also declares a **`tier`**: `0` runs synchronously and
  must be cheap (it blocks the prompt appearing — `/model`, `/help`); `1`
  runs as background warm-up after the prompt is already interactive
  (memory tiers, vector stores, retrieval, repository cognition,
  orchestration).
- **CLI bootstrap levels** — independently of subsystem tiers,
  `velune/cli/registry.py:CommandSpec.bootstrap` is either `"light"`
  (Typer/config only — used by `velune --version`, `velune config show`) or
  `"full"` (constructs the entire Tier-1 stack). This is the mechanism
  behind Velune CLI's near-zero-cost startup for simple commands — see
  [ARCHITECTURE.md § Process shape](ARCHITECTURE.md#1-process-shape).

**Practical implication for contributors:** if you add a new subsystem,
give it a `module.py` with a factory function and register it with the
`RuntimeBootstrapper`, rather than instantiating it inline wherever it's
used. Code that reaches for a service should go through `get_container()` /
`inject()`, not import and construct the class directly — otherwise you get
a second, unwired instance (see the `BoundedCouncilOrchestrator` /
`velune/cognition/agents/*` dead-code trap in
[ARCHITECTURE.md § Known rough edges](ARCHITECTURE.md#known-rough-edges-for-contributors),
which is exactly this mistake).

---

## 2. Module boundaries — what depends on what

Rough dependency direction (upper layers depend on lower ones, not the
reverse):

```text
cli/            ← REPL, commands, rendering — depends on everything below
orchestration/  ← native tool loop, alias over CouncilOrchestrator
cognition/      ← council pipeline — depends on memory, retrieval, repository
retrieval/      ← hybrid search — depends on memory (shares Qdrant client)
memory/         ← 5-tier storage — depends on core (paths, DI)
repository/     ← indexing/graph — depends on tools (filesystem), core
tools/          ← file/git/exec/web primitives — depends on execution, core
execution/      ← sandboxing — depends on core
providers/      ← model adapters — depends on core (keystore)
mcp/            ← server+client — depends on tools (exposes them), core
hooks/, plugins/, resources/  ← extension points — depend on core, wire into cli/ and mcp/
core/           ← paths, trust, retry, task registry, errors — depends on nothing else in-repo
```

If you find yourself importing from `cli/` inside `memory/` or `retrieval/`,
that's backwards — it means UI-layer concerns have leaked into a subsystem
that should be usable headlessly (e.g. from `velune mcp serve`, which has
no REPL at all).

> `intelligence/` (change detection → incremental reindex) and `knowledge/`
> (AI-queryable knowledge graph) sit alongside `repository/` at the same
> layer, and `recovery/` sits alongside `core/` as a cross-cutting concern
> (backup/restore/crash-recovery touches memory, sessions, config, and
> providers, so it depends on all of them but nothing depends on it).

---

## 3. Testing philosophy

Tests live in `tests/` (unit-style, one module per subsystem) and
`tests/integration/` (crosses subsystem boundaries — e.g. a full REPL
slash-command round-trip, or a plugin actually being scanned/loaded/enabled).

Some conventions worth knowing before adding tests:

- **Prefer real objects over mocks for the DI container and memory tiers.**
  Past incidents (see project history around the Qdrant/LanceDB dual-store
  split) came from tests mocking a store in a way that diverged from how
  the real store behaved — a passing mocked test hid a real integration
  bug. Where a real SQLite/Qdrant/LanceDB instance is cheap to spin up
  in-process, use it.
- **New subsystems should ship with both a unit test and, if they're wired
  into the REPL, at least one test that exercises them through the actual
  slash-command dispatch path** (`VeluneREPL._handle_slash_command`, or
  `build_slash_registry()` in `velune/cli/slash_dispatcher.py`), not just
  the underlying class in isolation — dispatch wiring bugs (a handler
  that's implemented but never registered) have been a recurring category
  of bug in this codebase.
- **Dead-code checks**: before extending a class, grep for its importers.
  This codebase has more than one instance of a "second implementation"
  (see the council agents note above) accumulating because a refactor added
  a new path without removing the old one. If your grep turns up zero
  importers outside the file itself and its own tests, you've likely found
  another one — flag it for removal rather than building on it.

Run the suite the same way CI does:

```bash
pytest tests/unit -q                                       # fast path
pytest tests/ -v                                            # full suite
pytest tests/ --cov=velune --cov-report=term-missing -q     # coverage
ruff check velune/                                           # lint (blocking)
ruff format --check velune/                                  # format (blocking)
pyright velune/                                               # type check (blocking)
```

> `pyright`, not `mypy`, is the type checker CI actually gates on (see the
> `lint` job below). `pyproject.toml`'s `[tool.pyright]` section runs in
> `basic` mode with most optional-access/argument-type checks turned off —
> it catches structural mistakes, not full strict typing.

---

## 4. CI pipeline

`.github/workflows/ci.yml` runs on every push/PR to `main` or `develop`,
gated by a final `ci-pass` aggregation job:

| Job | What it does |
| --- | --- |
| `lint` | `ruff check`, `ruff format --check`, and `pyright velune/` — all blocking |
| `security` | `pip-audit --skip-editable` (dependency vulnerabilities), `bandit` (static analysis, medium+ severity gates the build), a gitleaks secret scan (incremental on PRs — only the commits introduced by the push; full history on a fresh branch), plus two regression guards: no `shell=True` anywhere in `velune/`, and no more than one `asyncio.run()` call outside `plugins/runner.py` (a subprocess worker where it's intentional) |
| `test` | `pip install -e ".[all,dev]"` then `pytest`, across the matrix: Python 3.10–3.13 × Ubuntu/Windows/macOS |
| `build` | Builds sdist + wheel via **Hatchling** (`[build-system] requires = ["hatchling"]` in `pyproject.toml`), `twine check --strict`, verifies the wheel is pure-python (`py3-none-any` — no compiled extension required for the PyPI package), reproducible via `SOURCE_DATE_EPOCH` pinned to the commit date |
| `build-go` | Builds/tests/vets the optional Go launcher under `ext/go/cmd/velune`, Go 1.26, matrix across all three OSes |
| `build-rust` | Builds/tests the optional Rust native helpers under `ext/rust/velune-native/` (`cargo fmt --check`, `clippy -- -D warnings`, `cargo test --lib`), Rust stable, matrix across all three OSes |
| `install-smoke` | Downloads the `build` job's wheel, installs it into a clean environment (not the source tree), and runs `velune --version`, `velune --help`, `python -m velune --version`, `velune doctor check` — matrix across OS × Python version |
| `ci-pass` | Aggregation gate — fails if any of the above jobs didn't succeed; this is the single required status check for branch protection |

`.github/workflows/release.yml` handles the publish path (separate from CI,
triggered on tag push — see that file directly for the exact steps rather
than duplicating them here, since release mechanics change more often than
architecture).

**Key invariant the `build` job enforces:** the PyPI wheel must stay
pure-Python. The Go and Rust components under `ext/` are validated in CI
but are not required for `pip install velune-cli` to work — Velune CLI keeps
pure-Python fallbacks for anything the Rust helpers would otherwise
accelerate. Don't add a hard dependency on either native component to a
code path that's supposed to work on a bare `pip install`.

> There is no committed lockfile (no `uv.lock`, `poetry.lock`, or
> `requirements.txt`) — dependency resolution is `pyproject.toml` floors
> only, verified across the CI matrix rather than pinned. `[project.optional-dependencies].dev`
> is `ruff`, `pyright`, `pre-commit`, `pip-audit`, `bandit[toml]`, `build`,
> `twine`, `pytest`, `pytest-asyncio` — installed via `pip install -e ".[dev]"`.

---

## 5. Extension points — the design pattern behind all of them

Hooks, plugins, file-based slash commands, and MCP server declarations all
follow the same shape, deliberately: **external, declarative configuration
that gets loaded into an existing registry, rather than a new mechanism per
feature.** Concretely:

- A slash command, a hook binding, and an MCP server are all "data" (TOML,
  JSON) describing *what* to run and *when*, not code executed in-process
  by Velune CLI's own interpreter.
- All four load into the same core objects the built-ins use
  (`SlashCommandRegistry`, `HookDispatcher`, `MCPServerRegistry`) — so a
  plugin-provided command gets tab-completion, palette search, and `/help`
  listing for free, with no special-casing anywhere in the CLI layer.
- Loading happens once, at REPL startup (`PluginManager`,
  `FileCommandLoader`, `MCPServerRegistry.load_config`), not lazily per-use
  — so a broken plugin manifest fails loudly at startup instead of at some
  unpredictable later point.

If you're adding a new kind of extensibility, prefer extending one of these
three registries over inventing a fourth loading mechanism.

---

## 6. Debugging tips specific to this codebase

- **"My change to a handler isn't taking effect"** — check
  `slash_dispatcher.py:build_slash_registry()` actually points at the
  module/method you edited; handlers are bound directly to `VeluneREPL`
  methods (`handler=repl._cmd_yourcommand`) at registry-build time, so a
  typo in the method name fails at that call, not silently — but a command
  that's implemented on the REPL class and never registered in
  `build_slash_registry()` fails silently (it just never appears in
  `/help` or dispatch).
- **"Memory/retrieval results look stale or wrong"** — remember there are
  two vector stores (Qdrant for retrieval, LanceDB for memory lifecycle).
  Confirm which one the code path you're debugging actually reads from
  before assuming a shared cause.
  See [ARCHITECTURE.md § Memory system](ARCHITECTURE.md#5-memory-system).
- **"A council task got escalated to a higher tier and I don't know why"**
  — check the repository's import graph fan-in for whatever file was
  mentioned; `TierClassifier.classify()` (`cognition/council/tiers.py`)
  escalates independent of keywords when a mentioned file has many
  dependents (fan-in ≥ 5 floors the tier at `FULL`, ≥ 3 at `STANDARD`, ≥ 1
  at `MINIMAL`). This is intended behavior, not a bug, but it surprises
  people the first time they see it.
- **"Where does X actually get constructed?"** — grep for the factory
  method or `_create_X` function in the relevant `module.py` /
  `factory.py`, not for `class X` — construction is centralized in
  factories registered with the DI container (subsystems) or on
  `CouncilAgentFactory` (council agents/critics), not scattered at call
  sites.
- **Windows-specific issues** — this project treats Windows as a first-class
  platform (see the CI matrix). Command execution sandboxing, PATH
  resolution (`shutil.which` + trusted-path checks in
  `execution/command_spec.py`), and the OS keyring all have Windows-specific
  code paths; if you only test on macOS/Linux, run at least
  `pytest tests/ -v` under Windows (or check the CI matrix result) before
  assuming a fix generalizes.

---

## 7. Where things live, cross-referenced

| Question | Doc |
| --- | --- |
| What does the system look like end-to-end? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| What can I type in the REPL? | [SLASH_COMMANDS.md](SLASH_COMMANDS.md) |
| How do I use this well as an end user? | [USAGE_GUIDE.md](USAGE_GUIDE.md) |
| How does MCP client/server/trust work? | [MCP.md](MCP.md) |
| How do I add a provider / command / council agent, step by step? | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| What's the threat model and trust boundary? | [SECURITY.md](../SECURITY.md) |
