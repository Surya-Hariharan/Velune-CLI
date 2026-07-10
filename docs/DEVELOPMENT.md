# Development Guide

This is the internal engineering reference: how the codebase is put
together at the framework level, how CI validates it, and how to reason
about changes that cross subsystem boundaries. For "how do I set up a venv
and open a PR," see [CONTRIBUTING.md](../CONTRIBUTING.md) — that doc owns
the step-by-step contributor workflow; this one owns the *why* behind the
structure so those steps make sense.

---

## 1. The bootstrap / DI layer

Everything in Velune beyond the Typer command dispatch runs on top of a
small dependency-injection kernel in `velune/kernel/`:

- **`ServiceContainer`** (`kernel/registry.py`) — a registry of named
  services with lazy construction: a service is only built the first time
  something asks for it, and the same instance is reused after that.
  `get_container()` returns the process-wide singleton; `inject(name)` is
  the decorator/lookup helper used throughout the codebase instead of
  constructing subsystems directly.
- **`RuntimeEnvironment`** (`kernel/bootstrap.py`) — the object every
  subsystem module receives to register its services against. It carries
  workspace path, config, and hardware profile.
- **`SubsystemModule`** (`kernel/bootstrap.py`) — the unit of registration.
  Each major package (`memory`, `retrieval`, `cognition`, `mcp`, `resources`,
  `hooks`, `plugins`, …) exposes a `module.py` with factory functions
  (`_create_*`) that `RuntimeBootstrapper` wires into the container.
- **Bootstrap levels** — `velune/cli/registry.py:CommandSpec.bootstrap` is
  either `"light"` (Typer/config only — used by `velune --version`,
  `velune config show`) or `"full"` (constructs the entire Tier-1 stack:
  memory, retrieval, cognition, orchestration). This is the mechanism behind
  Velune's near-zero-cost startup for simple commands — see
  [ARCHITECTURE.md § Process shape](ARCHITECTURE.md#1-process-shape).

**Practical implication for contributors:** if you add a new subsystem,
give it a `module.py` with a factory function and register it in the
relevant `MODULES` tuple, rather than instantiating it inline wherever it's
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

```
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
  slash-command dispatch path** (`VeluneREPL._handle_slash_command` or the
  relevant handler), not just the underlying class in isolation — dispatch
  wiring bugs (a handler that's implemented but never registered) have been
  a recurring category of bug in this codebase.
- **Dead-code checks**: before extending a class, grep for its importers.
  This codebase has more than one instance of a "second implementation"
  (see the council agents note above) accumulating because a refactor added
  a new path without removing the old one. If your grep turns up zero
  importers outside the file itself and its own tests, you've likely found
  another one — flag it for removal rather than building on it.

Run the suite the same way CI does:

```bash
pytest tests/unit -q                                    # fast path
pytest tests/ -v                                         # full suite
pytest tests/ --cov=velune --cov-report=term-missing -q  # coverage
ruff check velune/                                        # lint gate (blocking)
mypy velune/ --ignore-missing-imports                      # type check (informational only, not yet a gate)
```

---

## 4. CI pipeline

`.github/workflows/ci.yml` runs on every push/PR:

| Job | What it does |
| --- | --- |
| `lint` | `ruff check` — blocking |
| `security` | Gitleaks secret scan (incremental on PRs, full on `main`) |
| `test` | Full `pytest` run across the matrix: Python 3.10–3.13 × Ubuntu/Windows/macOS |
| `build` | Builds sdist + wheel, `twine check --strict`, verifies the wheel is pure-python (`py3-none-any` — no compiled extension is required for the PyPI package), reproducible via `SOURCE_DATE_EPOCH` pinned to the commit date |
| `build-go` | Builds/tests/vets the optional Go launcher under `ext/go/` (`cmd/velune`), matrix across all three OSes |
| `build-rust` | Builds/tests the optional Rust native helpers under `ext/rust/velune-native/` (clippy + rustfmt), matrix across all three OSes |

`.github/workflows/release.yml` handles the publish path (separate from CI,
triggered on tag push — see that file directly for the exact steps rather
than duplicating them here, since release mechanics change more often than
architecture).

**Key invariant the `build` job enforces:** the PyPI wheel must stay
pure-Python. The Go and Rust components under `ext/` are validated in CI
but are not required for `pip install velune-cli` to work — Velune keeps
pure-Python fallbacks for anything the Rust helpers would otherwise
accelerate. Don't add a hard dependency on either native component to a
code path that's supposed to work on a bare `pip install`.

---

## 5. Extension points — the design pattern behind all of them

Hooks, plugins, file-based slash commands, and MCP server declarations all
follow the same shape, deliberately: **external, declarative configuration
that gets loaded into an existing registry, rather than a new mechanism per
feature.** Concretely:

- A slash command, a hook binding, and an MCP server are all "data" (TOML,
  JSON) describing *what* to run and *when*, not code executed in-process
  by Velune's own interpreter.
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
  module/function you edited; handlers are lazily imported by string
  reference (`module_path`, `attr_name`), so a typo there fails silently at
  dispatch time rather than at import time.
- **"Memory/retrieval results look stale or wrong"** — remember there are
  two vector stores (Qdrant for retrieval, LanceDB for memory lifecycle).
  Confirm which one the code path you're debugging actually reads from
  before assuming a shared cause.
  See [ARCHITECTURE.md § Memory system](ARCHITECTURE.md#5-memory-system).
- **"A council task got escalated to FULL tier and I don't know why"** —
  check the repository's import graph fan-in for whatever file was
  mentioned; `TierClassifier` escalates independent of keywords when a
  mentioned file has many dependents. This is intended behavior, not a bug,
  but it surprises people the first time they see it.
- **"Where does X actually get constructed?"** — grep for `_create_X` in
  the relevant `module.py`, not for `class X` — construction is centralized
  in factory functions registered with the DI container, not scattered at
  call sites.
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
