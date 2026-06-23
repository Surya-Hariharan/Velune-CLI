# Architecture — Instant Startup & On-Demand Cognition

This document describes the two-phase model introduced by the startup refactor:
a **lean, instant launch path** and a separate **explicit, user-driven cognition
path**. They never overlap — nothing on the cognition path runs at startup.

## 1. Startup path (instant, <500ms target)

```
                      ┌──────────────────────────────────────────┐
   velune  ──────────▶│ main()  (velune/main.py)                  │
                      │   • --version / --help fast-paths         │
                      └──────────────────┬───────────────────────┘
                                         ▼
                      ┌──────────────────────────────────────────┐
                      │ build_runtime()  (velune/core/runtime.py) │
                      │   • config + logging + console            │
                      │   • module bootstrap (factories only)     │
                      └──────────────────┬───────────────────────┘
                                         ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ REPL handoff (velune/cli/app.py → kernel/entrypoint.py)               │
   │   • lifecycle.startup() → RepositoryCognitionService.initialize()     │
   │       └── NO-OP  ◀── the critical change (cognition.py)               │
   │   • _restore_active_model()   (no network — reads active_model.json)  │
   │   • _show_welcome_guide()     (first launch, no model configured)     │
   │   • _detect_repo_marker()     (advisory hint ONLY — never scans)      │
   └─────────────────────────────────────────────────────────────────────┘
                                         ▼
                                   PROMPT VISIBLE

   Allowed at startup: config, settings, command registry, UI, model
   registry (lazy), lightweight memory metadata, restore default model.
   Forbidden at startup: scanning, cognition, embeddings, indexing,
   chunking, graph/architecture/dependency analysis, RAG/vector build.
```

## 2. On-demand cognition path (explicit)

```
   /project open <path>          register + activate workspace (no cognition)
          │                       WorkspaceRegistry → ~/.velune/workspaces.json
          ▼
   /cognition quick              quick_summary()   manifests only      (2–5s)
   /cognition standard|init      run_incremental() file-level symbols
   /cognition deep|rebuild       run_deep()        symbols+graph+arch
          │
          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ guards (cli/repl.py)                                           │
   │   1. _cognition_model_ready()   → Rule 4: refuse if no model   │
   │   2. cog.unsafe_reason()        → Rule 12: refuse home/root    │
   └───────────────┬──────────────────────────────────────────────┘
                   ▼
   preview()  ──▶  _confirm_cognition() panel  ──▶  [Y/N]
   (file count,      Workspace / Mode / Files /
    est. tokens)     Est. Tokens / Cost / Duration
                   │ confirmed
                   ▼
   _submit_cognition_job()  → JobRegistry + track()  (background)
          │
          ▼
   /cognition status   table of cognition jobs (phase, result)
   /cognition cancel   cancels the running job
   /jobs · /dashboard  shared job views
```

## 3. Safety model

- **`unsafe_index_root_reason(root)`** (`velune/repository/scanner.py`) returns a
  human-readable reason — `"your home directory"` / `"a filesystem root"` — for
  roots that must never be recursively crawled, else `None`. Consulted by
  `RepositoryCognitionService.unsafe_reason()` and `probe_for_changes()`, and by
  every `/cognition` subcommand before any work begins.
- **`_detect_repo_marker(path)`** (`velune/cli/app.py`) looks for `.git`,
  `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod` and prints an
  *advisory* hint ("Repository detected → run `/project open .`"). It never reads
  contents or triggers indexing.

## 4. Model registry, discovery & persistence

```
   /model discover ─▶ ModelCapabilityRegistry.refresh()
                         └─ ModelDiscoveryScanner.scan_all()
                              ├─ Ollama        :11434  (reachability-gated)
                              ├─ LM Studio     :1234   (reachability-gated)
                              ├─ OpenAI-compat :8000/:8080/:3000 (gated)  ◀ Rule 7
                              └─ cloud providers (only if API key in keystore)
                         picker ─▶ _activate_model()
                                      ├─ save_active_model() → ~/.velune/active_model.json
                                      └─ providers.default_provider → velune.toml

   next launch ─▶ _restore_active_model()  (reads active_model.json, no network)
```

- Discovery is **by model name only** (Rule 6) — `/api/tags` for Ollama,
  `/v1/models` for LM Studio and OpenAI-compatible servers. No filesystem,
  blob, manifest or sha256 paths are ever requested.
- The `openai-compat` provider/adapter pair lets self-hosted OpenAI-compatible
  servers (vLLM, LocalAI, llama.cpp `server`, …) be discovered *and* used; the
  discoverer records each model's endpoint in `metadata["base_url"]`, and the
  adapter's `base_url` defaults to `providers.openai_compat.base_url`.

## Component map

| Concern | Module |
|---------|--------|
| Lean launch / banner / welcome | `velune/cli/app.py`, `velune/core/runtime.py` |
| Lifecycle (inert cognition init) | `velune/kernel/lifecycle.py`, `velune/repository/cognition.py` |
| Cognition entry points | `velune/repository/cognition.py` (+ `scanner.py`, `incremental_indexer.py`) |
| `/cognition`, `/project`, `/model` handlers | `velune/cli/repl.py`, `velune/cli/slash_dispatcher.py` |
| Workspace registry | `velune/cli/workspaces.py` |
| Model registry & discovery | `velune/models/registry.py`, `velune/providers/discovery/*` |
| Model persistence | `velune/cli/model_prefs.py` |
| Background jobs | `velune/core/task_registry.py` (`JobRegistry`, `track()`) |
