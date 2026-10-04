# Development Guide

*How the codebase is put together at the framework level, how CI validates it, and how to reason about
changes that cross subsystem boundaries.*

For "how do I set up a venv and open a PR," see [CONTRIBUTING.md](../CONTRIBUTING.md) — that doc owns the
step-by-step contributor workflow; this one owns the *why* behind the structure so those steps make sense.

---

## Contents

- [1. The bootstrap / DI layer](#1-the-bootstrap--di-layer)
- [2. Module boundaries — what depends on what](#2-module-boundaries--what-depends-on-what)
- [3. Testing](#3-testing)
- [4. CI pipeline](#4-ci-pipeline)
- [5. Extension points — the design pattern behind all of them](#5-extension-points--the-design-pattern-behind-all-of-them)
- [6. Debugging tips specific to this codebase](#6-debugging-tips-specific-to-this-codebase)
- [7. Where things live, cross-referenced](#7-where-things-live-cross-referenced)

---

## 1. The bootstrap / DI layer

Everything in Velune CLI beyond the Typer command dispatch runs on top of a small dependency-injection
kernel in `velune/kernel/`:

- **`ServiceContainer`** (`kernel/registry.py`) — a registry of named services (`runtime.<name>` keys)
  with lazy factories, concrete instances (`register_instance`), hot-swapping (`hot_swap`) and readiness
  gates (`mark_ready`, `await wait_ready(key, timeout)`) for services that finish loading in the
  background.
- **`RuntimeEnvironment`** (`kernel/bootstrap.py`) — what every subsystem factory receives: workspace
  path, config, the container and the lifecycle coordinator.
- **`SubsystemModule`** (`kernel/bootstrap.py`) — the unit of registration:
  `SubsystemModule(name, factory, container_key, lifecycle_key=None, dependencies=[], tier=1)`. Each major
  package exposes its modules in a **`subsystems.py`** file (`PROVIDER_MODULES`, `MEMORY_MODULES`,
  `TOOL_MODULES`, …); `RuntimeBootstrapper` topologically sorts them by `dependencies` and wires them in.
  - A module with a `lifecycle_key` is lifecycle-critical: its `initialize()`/`shutdown()` are driven by
    `LifecycleCoordinator`, and a factory failure aborts startup. A module without one is optional — a
    failure is logged and the module skipped. A factory that returns `None` is treated as "not provided".
  - **`tier`** — `0` runs synchronously and must be cheap (it delays the first prompt: providers, models,
    the event bus, observability); `1` is background warm-up after the prompt is interactive (memory,
    retrieval, repository cognition, tools, the council, the knowledge graph, the intelligence engine).
    Tier-1 imports are deferred in `kernel/modules.py:load_background_modules()` so heavy package imports
    never run on the startup path.
- **CLI bootstrap levels** — independently of subsystem tiers, `velune/cli/registry.py:CommandSpec.
  bootstrap` is `"light"` (Tier 0 only — e.g. `velune config show`) or `"full"` (Tier 0 and Tier 1).
  `velune --version` and `--help` never build a runtime at all.

**Practical implication:** if you add a subsystem, give it a factory in its package's `subsystems.py` and
register the module list with the bootstrapper, rather than instantiating it inline where it is used.
Reach services through the container you were handed (`repl.container`, `cli_context.container`,
`env.container`) — the process-wide `get_container()` is a *separate* instance that the runtime does
**not** populate, so code that looks services up there silently gets nothing in a real session.

---

## 2. Module boundaries — what depends on what

There is no enforced layering; this is the *intended* direction (higher layers depend on lower ones):

```text
cli/            ← REPL, commands, rendering — depends on almost everything below
cognition/      ← the Reasoning Council — depends on providers, memory, models, execution
orchestration/  ← native tool loop (+ an alias of the council orchestrator)
retrieval/      ← hybrid search — depends on repository/knowledge data and the memory stores
memory/         ← tiered conversational memory — depends on core/kernel
intelligence/   ← change detection → incremental reindex; drives knowledge/
repository/ knowledge/  ← indexing, import graph, knowledge graph
tools/          ← file / git / exec / web primitives — depends on execution, core
execution/      ← sandboxing, command validation, diff preview
providers/ models/  ← model adapters, credentials, discovery, capability scoring
mcp/            ← MCP client + server — exposes the tool registry
hooks/ plugins/ resources/ integrations/  ← extension points
kernel/ core/   ← DI, config, paths, trust, retry, task registry, errors
```

In practice a few packages reach "upward" — `cognition/`, `context/` and `proactive/` import helpers from
`cli/` — so treat the diagram as a guide, not a guarantee, and prefer not to add new upward imports.
Subsystems used headlessly (for example `velune mcp serve`, which has no REPL) should not import `cli/`.

`recovery/` (backup/restore) sits beside `core/` as a cross-cutting concern; `observability/`, `telemetry/`
and `proactive/` observe the rest of the system via the event bus (`velune/events.py`).

---

## 3. Testing

Tests live in `tests/` (one module per concern) and `tests/integration/` (cross-subsystem). There is no
`tests/unit` directory.

Conventions worth knowing before adding tests:

- **Prefer real objects over mocks for storage.** A passing mocked store has hidden real integration bugs
  before. Where a real SQLite/LanceDB/Qdrant instance is cheap in-process, use it (`tmp_path`).
- **Test wiring, not just classes.** A feature wired into the REPL should have a test that goes through the
  registry (`build_slash_registry()` in `velune/cli/slash_dispatcher.py`) or the actual dispatch — a handler
  that is implemented but never registered has been a recurring bug.
- **Dead-code check:** before extending a class, grep for its importers. If nothing outside the file and its
  own tests imports it, it is probably superseded — flag it rather than building on it.
- **Isolate config.** Config and the default provider are discovered by walking up from the working
  directory for a `velune.toml`; run tests from a temp dir (`monkeypatch.chdir(tmp_path)`) and never leave a
  stray `velune.toml` in a parent folder.
- **Doc tests:** `tests/test_slash_commands_doc.py` fails when `docs/slash-commands.md` drifts from the
  registered commands, and `tests/test_docs_links.py` fails on a broken relative link in the Markdown docs.

Run the same checks CI does:

```bash
pytest                              # full suite (CI runs plain `pytest`)
pytest -k provider -q               # a slice
pytest --cov=velune --cov-report=term-missing -q   # coverage
ruff check velune/                  # lint (blocking)
ruff format --check velune/         # format (blocking)
pyright velune/                     # type check (blocking)
```

> `pyright`, not `mypy`, is the type checker CI gates on. `[tool.pyright]` in `pyproject.toml` runs in
> `basic` mode — it catches structural mistakes, not full strict typing. Lint and type checks cover
> `velune/` only, not `tests/`.

> **Windows:** the sandbox refuses executables that resolve outside trusted locations (system roots, the
> running interpreter's environment, a `.venv`/`venv` directly under the workspace). If sandbox/process
> tests fail locally with "resolved to untrusted path", put your virtualenv's `Scripts` directory first on
> `PATH`.

---

## 4. CI pipeline

`.github/workflows/ci.yml` runs on every push/PR to `main` or `develop`, gated by a final `CI Pass`
aggregation job:

| Job | What it does |
| --- | --- |
| **Lint** | `ruff check velune/`, `ruff format --check velune/`, `pyright velune/` — all blocking |
| **Security** | `pip-audit --skip-editable`; `uv lock --check` (the committed `uv.lock` must match `pyproject.toml`); `bandit` (medium+ severity/confidence gates the build, plus a low-severity count baseline that may not grow); a gitleaks secret scan; regression guards: no `shell=True` in `velune/`, `create_subprocess_shell` only at allow-listed sites, and no new `asyncio.run()` call sites |
| **Tests** | `pip install -e ".[all,dev]"` then `pytest`, across Python 3.10–3.13 × Ubuntu / Windows / macOS |
| **Build & Validate Artifacts** | Hatchling sdist + wheel (reproducible via `SOURCE_DATE_EPOCH`), `twine check --strict`, and the wheel must be pure-Python (`py3-none-any`) |
| **Go Launcher** | builds/tests/vets the optional Go launcher under `ext/go/` on all three OSes |
| **Rust Native** | `cargo fmt --check`, `clippy -D warnings`, `cargo test` for the optional helpers under `ext/rust/velune-native/` on all three OSes |
| **Wheel Install + REPL Smoke** | installs the built wheel into a clean environment and runs `velune --version`, `velune --help`, `python -m velune --version`, `velune doctor check` across OS × Python |
| **CI Pass** | fails if any of the above failed — the single required status check |

CodeQL runs separately. `.github/workflows/release.yml` handles publishing (tag-triggered; it verifies the
tag matches the package version, builds, publishes to PyPI and creates the GitHub release) — read that file
for the exact steps.

**Invariant:** the PyPI wheel stays pure-Python. The Go and Rust components under `ext/` are validated in CI
but are optional; every accelerated path has a pure-Python fallback, so don't add a hard dependency on
either to a path that must work after a bare `pip install velune-cli`.

> Dependency versions are pinned in the committed `uv.lock` (check with `uv lock --check`; refresh with
> `uv lock --upgrade-package <name>`). `[project.optional-dependencies]` defines `rag`, `parsing`,
> `telemetry`, `git`, `gguf`, `docker`, `all`, and `dev`.

---

## 5. Extension points — the design pattern behind all of them

Hooks, plugins, file-based slash commands, and MCP server declarations share one shape: **declarative
configuration loaded into an existing registry**, rather than a new mechanism per feature.

- A slash command, a hook binding and an MCP server are all data (TOML/JSON/Markdown) describing *what* to
  run and *when*, not code executed in Velune's own interpreter.
- They load into the same objects the built-ins use (`SlashCommandRegistry`, `HookDispatcher`,
  `MCPServerRegistry`), so a plugin-provided command gets tab-completion, palette search and `/help` for
  free.
- Loading happens at REPL startup (`PluginManager`, `FileCommandLoader`, `MCPServerRegistry`), so a broken
  manifest fails loudly at startup.

Where each one lives:

| Extension | Config | Code |
| --- | --- | --- |
| Slash commands (TOML) | `~/.velune/commands/`, `<workspace>/.velune/commands/` | `cli/commands/file_commands.py` |
| Plugins | `<workspace>/.velune/plugins/<name>/` and `~/.velune/plugins/<name>/`, each with `.velune-plugin/plugin.json` (+ `commands/`, `skills/`, `hooks/hooks.json`, `.mcp.json`) | `plugins/` |
| Hooks | `<workspace>/.velune/hooks.json`, `~/.velune/hooks.json`, `velune.toml [hooks]` | `hooks/` |
| MCP servers | `.mcp.json`, `~/.mcp.json`, `velune.toml [mcp.servers]` — see [mcp.md](mcp.md) | `mcp/` |
| Resource connectors | `velune.toml [resources.*]` | `resources/` |

Prefer extending one of these registries over inventing a new loading mechanism.

---

## 6. Debugging tips specific to this codebase

- **"My change to a handler isn't taking effect"** — check that `build_slash_registry()` in
  `velune/cli/slash_dispatcher.py` points at the method you edited. Handlers are bound to `VeluneREPL._cmd_*`
  delegators, which lazily import the real function from `velune/cli/handlers/`. A command implemented but
  never registered silently never appears in `/help`.
- **"Output corrupts the screen"** — the full-screen app owns the terminal. Anything printed to the real
  stdout/stderr (a module-level `Console()`, `print`, a logging stream handler) tears it apart. Print
  through `repl.console`, and use `asyncio.to_thread` for blocking work so the UI keeps repainting.
- **"Memory/retrieval results look stale or wrong"** — there are two vector stores (Qdrant for code
  retrieval, LanceDB for conversational memory) and three unrelated "graphs" (memory graph tier, repository
  import graph, knowledge graph). Confirm which one the code path reads before assuming a shared cause.
- **"A council task escalated to a higher tier"** — `TierClassifier.classify()`
  (`cognition/council/tiers.py`) raises the tier when a mentioned file has many dependents in the import graph
  (fan-in ≥ 5 → `FULL`, ≥ 3 → `STANDARD`, ≥ 1 → `MINIMAL`), independent of keywords. Intended behaviour.
- **"Where does X get constructed?"** — look for the `_create_X` factory in the package's `subsystems.py`
  (services) or on `CouncilAgentFactory` (council seats), not for `class X`.
- **"The model list is wrong / a model 404s"** — OpenAI, Anthropic, Gemini and Groq reconcile their curated
  model lists with the provider's live `/models`; others still use static lists. Run `velune models refresh`.
- **Windows** — Windows is a first-class platform (see the CI matrix). Sandbox PATH resolution
  (`execution/command_spec.py`), the OS keyring and DPAPI-protected files have Windows-specific paths; don't
  assume a macOS/Linux-only test run generalises.

---

## 7. Where things live, cross-referenced

| Question | Doc |
| --- | --- |
| What can I type in the REPL? | [slash-commands.md](slash-commands.md) |
| How do I use this well as an end user? | [usage-guide.md](usage-guide.md) |
| How does MCP client/server/trust work? | [mcp.md](mcp.md) |
| How do I add a provider / command / council agent, step by step? | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| What is the threat model and trust boundary? | [SECURITY.md](../SECURITY.md) |
| Who made this and how was it built over time? | [project-origin.md](project-origin.md), [AUTHORS.md](../AUTHORS.md) |
