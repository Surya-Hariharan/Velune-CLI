<div align="center">
  <img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/logo.png" alt="Velune Logo" width="100" />
  <h1>Startup Architecture Refactor — Migration Report</h1>
</div>

## Why this change

Running `velune` used to begin repository detection, scanning, cognition,
embedding generation, memory loading and architecture mapping **before** the CLI
appeared. This was slow, surprising, and — when Velune was launched from a home
directory or drive root (e.g. `C:\Users\<name>`) — it would crawl an unbounded,
unrelated tree (OneDrive, Documents, AppData…) before the prompt ever showed.

Startup is now **instant** and does **zero** repository processing. Cognition
became an explicit, user-driven workflow gated behind an open workspace and a
configured model.

## Before / after startup flow

```mermaid
flowchart LR
    subgraph Before
        direction TB
        B1([velune]) --> B2[load config]
        B2 --> B3[detect repository]
        B3 --> B4[scan repository]
        B4 --> B5[build cognition]
        B5 --> B6[generate embeddings]
        B6 --> B7[load memory]
        B7 --> B8[build architecture map]
        B8 --> B9([display CLI])
    end
    
    subgraph After
        direction TB
        A1([velune]) --> A2[load config / settings]
        A2 --> A3[load command registry + UI]
        A3 --> A4[load model registry lazy]
        A4 --> A5[restore default model]
        A5 --> A6[advisory repo hint]
        A6 --> A7([REPL prompt  ◀── <500ms])
        
        A7 -.->|Cognition is now explicit| A8([/project open])
        A8 --> A9([/cognition quick|standard|deep])
        A9 --> A10([preview -> confirm -> bg job])
    end
    
    style B4 fill:#ff4757,stroke:#ff6b81,stroke-width:2px,color:#fff
    style B5 fill:#ff4757,stroke:#ff6b81,stroke-width:2px,color:#fff
    style B6 fill:#ff4757,stroke:#ff6b81,stroke-width:2px,color:#fff
    style B8 fill:#ff4757,stroke:#ff6b81,stroke-width:2px,color:#fff
    style A7 fill:#2ed573,stroke:#7bed9f,stroke-width:2px,color:#fff
```

The key mechanical change: `RepositoryCognitionService.initialize()` — the
lifecycle hook invoked at startup — is now a **no-op**. All indexing happens only
through the manual entry points the `/cognition` command calls.

## Files added / modified / refactored

### Refactored (behavior changed)

| File | What changed |
|------|--------------|
| `velune/repository/cognition.py` | `initialize()` is now inert (no background indexing). Added manual entry points: `quick_summary()`, `preview()`/`_preview_sync()`, `run_incremental()`, `run_deep()`, `unsafe_reason()`. `probe_for_changes()` now refuses unsafe roots. |
| `velune/repository/scanner.py` | New `unsafe_index_root_reason(root)` guard — flags the home directory and filesystem/drive roots so they are never recursively indexed. |
| `velune/cli/repl.py` | Added `/model` subcommands (`discover\|connect\|use\|list\|status\|remove`), `/cognition` handler (`init\|quick\|standard\|deep\|status\|cancel\|rebuild`) with preview/confirm + background-job submission, and `/project open\|close\|status`. Restores persisted default model on startup (no network). |
| `velune/cli/slash_dispatcher.py` | Registers `/cognition`; updates `/project` and `/model` usage/descriptions. |
| `velune/cli/app.py` | Advisory `_detect_repo_marker()` (hint only, never scans) and first-launch `_show_welcome_guide()`. |
| `velune/cli/autocomplete.py` | `/cognition` categorized under *Workspace*. |
| `velune/models/registry.py` | `ModelCapabilityRegistry.remove()` for `/model remove`. |
| `velune/providers/discovery/scanner.py` | Registers `OpenAICompatDiscovery`, gated on reachability (Rule 7). |
| `velune/providers/registry.py` | Registers the `openai-compat` adapter factory (config-driven `base_url`). |
| `velune/kernel/config.py` | New `ProvidersConfig.openai_compat` entry (default `http://localhost:8000/v1`). |

### Added

| File | Purpose |
|------|---------|
| `velune/cli/model_prefs.py` | Persists the active model to `~/.velune/active_model.json` (atomic write); restored on startup with no network discovery. |
| `velune/providers/discovery/openai_compat.py` | Discovers generic OpenAI-compatible local servers on `:8000/:8080/:3000` via `GET /v1/models` (Rule 7). |
| `velune/providers/adapters/openai_compat.py` | OpenAI-compatible local provider adapter (chat/stream/embed), `base_url`-parameterized. |
| `tests/test_cognition_manual.py` | Manual cognition surface: `quick_summary`/`preview`/`run_incremental`. |
| `tests/test_workspace_index_guard.py` | Startup never indexes; unsafe-root guard refuses home/drive roots. |
| `tests/test_discovery_openai_compat.py` | OpenAI-compatible discovery (no network dependency). |

## Behavioral changes & backward compatibility

- **Cognition no longer runs automatically.** Anything that relied on an index
  existing at startup must now run `/cognition` first. Incremental state lives in
  `<workspace>/.velune/index_state.json`; a second run with no changes is a no-op.
- **`/model` and `/project` are backward compatible.** Both keep their legacy
  bare-argument forms (`/model <id>` switches directly; `/project <name|path>`
  switches). New subcommands are additive.
- **Opening a project never triggers cognition** — `/project open` only registers
  and activates the workspace (Rule 3).
- **Cognition requires a model** (Rule 4) and **refuses unsafe roots** (Rule 12);
  it prints actionable guidance instead of crawling.
- **New config field** `providers.openai_compat.base_url` is optional and defaults
  to `http://localhost:8000/v1`; existing `velune.toml` files need no changes.
- **Discovery uses model names only** (Rule 6) — `ollama list` / HTTP endpoints;
  users never supply blob/manifest/sha256 paths.

## Verification

- `pytest` → **361 passed, 2 skipped**.
- `velune --version` returns instantly (lazy entry point preserved).
- New-surface tests: `tests/test_cognition_manual.py`,
  `tests/test_workspace_index_guard.py`, `tests/test_discovery_openai_compat.py`.
