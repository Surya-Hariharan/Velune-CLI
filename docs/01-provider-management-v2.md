# Provider Management: v1 Reverse-Engineering & v2 Design

*A factual map of the provider subsystem as it exists today, its execution
path from `/provider add anthropic` to a first streamed token, its weaknesses,
and a proposed v2 architecture with a phased migration plan.*

This is a design document, not an implementation — no code changes accompany
it. See [architecture.md § 3](architecture.md#3-providers) for the one-paragraph
summary this document supersedes in depth.

---

## Contents

- [1. v1: what exists today](#1-v1-what-exists-today)
- [2. Execution graph: `/provider add anthropic` → first streamed token](#2-execution-graph-provider-add-anthropic--first-streamed-token)
- [3. v1 architecture evaluation](#3-v1-architecture-evaluation)
- [4. Provider Management v2 — subsystem design](#4-provider-management-v2--subsystem-design)
- [5. v1 vs v2 comparison](#5-v1-vs-v2-comparison)
- [6. Phased migration roadmap](#6-phased-migration-roadmap)

---

## 1. v1: what exists today

Package map (`velune/providers/`, `velune/cli/{commands,handlers,onboarding}`):

```text
velune/providers/
  base.py                 ModelProvider protocol
  registry.py               ProviderRegistry — factories, keyed adapters
  catalog.py                 ProviderMeta — display-metadata source of truth (partial)
  manager.py                  ProviderManager — init lifecycle with backoff
  router.py                    ProviderRouter — capability routing + offline fallback
  health.py                     InternetConnectivityChecker (30s TTL TCP probe)
  health_monitor.py              ProviderHealthMonitor — 30s background polling
  verifier.py                     reverify() / reverify_stale()
  validation.py                    validate_provider() — live per-provider API check
  keystore.py                       CredentialManager — encrypted credentials.json
  crypto.py                          AES-256-GCM, OS keyring / passphrase / machine-key
  retrying.py                         RetryingProvider — retry + concurrency semaphore
  adapters/*.py                        20 provider clients (14 cloud + 6 local/generic)
  discovery/*.py                        16 model-listing backends

velune/cli/
  provider_ui.py            ProviderPalette — REPL /providers, /connect flows
  commands/providers.py       `velune provider ...` Typer sub-app (parallel implementation)
  commands/doctor.py            provider health checks
  handlers/providers.py           /providers, /connect slash-command entry points
  handlers/model.py                 /model, /models — active selection & persistence
  repl.py                              _handle_prompt cross-provider fallback loop
```

Two things to flag before the detail: **there are three separate provider
metadata tables** (`providers/catalog.py::_PROVIDERS`, `cli/commands/providers.py::_PROVIDER_META`,
and `registry.py`'s own hardcoded factory list), and **two separate
implementations of "add a provider"** (the REPL's `ProviderPalette` and the
Typer `velune provider add` command). Both facts recur throughout the
weaknesses in §3 — most of v1's inconsistencies trace back to these two
forks rather than to any single bad line of code.

---

## 2. Execution graph: `/provider add anthropic` → first streamed token

```text
USER TYPES: /provider add anthropic
│
├─ slash_dispatcher.py:204-213 — "provider"/"providers"/"prov" alias resolved
├─ VeluneREPL._cmd_providers            repl.py:1825-1828
├─ cmd_providers(repl, args)            cli/handlers/providers.py:20-32
│     → ProviderPalette(...).run("add anthropic")
├─ ProviderPalette.run()                cli/provider_ui.py:156-177
│     sub="add", rest="anthropic" (non-empty → skip interactive picker)
│     → self._connect("anthropic")
│
├─ ProviderPalette._connect("anthropic")   cli/provider_ui.py:223-292
│  │
│  ├─ catalog.get("anthropic")             providers/catalog.py
│  │     requires_key=True → proceed (local providers short-circuit here)
│  │
│  ├─ LOOP: text_input widget, password=True (key hidden on screen)
│  │     _validate_key_shape()             provider_ui.py:130-142
│  │     (client-side only: rejects empty / whitespace-containing paste)
│  │
│  ├─ run_with_status(validate_provider("anthropic", key))
│  │     providers/validation.py:719-740
│  │     ├─ asyncio.wait_for(..., timeout=15.0)
│  │     └─ _validate_anthropic()          validation.py:117-168
│  │           httpx.AsyncClient(timeout=10.0)
│  │           POST https://api.anthropic.com/v1/messages  (1-token probe —
│  │           Anthropic has no free /models endpoint to ping instead)
│  │           maps HTTP status → ValidationStatus{OK, INVALID_KEY,
│  │           EXPIRED_KEY, RATE_LIMITED, NETWORK_ERROR, ...}
│  │
│  ├─ [validation fails] → _FAILURE_HINTS shown, submenu:
│  │     retry / save-anyway (unverified) / cancel      provider_ui.py:68-83
│  │
│  └─ [validation OK] ─────────────────────────────────────────────┐
│                                                                    │
│     ├─ CredentialManager.save_key("anthropic", key, verified=True)│
│     │     providers/keystore.py                                  │
│     │     ├─ merge into in-memory {"providers": {...}} dict       │
│     │     ├─ JSON-serialize whole store                          │
│     │     ├─ encrypt_credentials()  AES-256-GCM  crypto.py:296-333│
│     │     │     master key: OS keyring → passphrase (PBKDF2-HMAC- │
│     │     │     SHA256, 390k iters) → machine-ID fallback (warns) │
│     │     ├─ atomic write: temp file → fsync → os.replace         │
│     │     │     keystore.py:194-228, keeps .bak, chmod 0600 /     │
│     │     │     icacls on Windows                                 │
│     │     └─ bump key_revision (invalidates any cached adapter)   │
│     │                                                             │
│     ├─ mark_verified("anthropic")  → KeyState.VERIFIED,           │
│     │     last_verified=now()      keystore.py:394-412            │
│     │                                                             │
│     ├─ _report_saved()  — prints "connected" + storage path        │
│     │     provider_ui.py:294-307                                    │
│     │                                                                │
│     └─ await self._discover_one("anthropic")                         │
│           → discovery/anthropic.py backend runs, registers            │
│             ModelDescriptors into runtime.model_registry               │
│                                                                          │
└─ /provider add anthropic COMPLETES HERE.
   No token has been streamed yet — adding a provider only proves a
   lightweight validation call succeeded, not that real inference works.
   A streamed token requires a SEPARATE follow-up action:

SEPARATE PATH: user sends an actual prompt (or the anthropic model was/becomes active)
│
├─ activate_model(repl, model)          cli/handlers/model.py:625-647
│     (via /model use <anthropic-model>, or first-registered-model fallback
│     in _resolve_active_model_and_provider, repl.py:1005-1024)
│     sets repl.active_model, persists prefs (model_prefs.py),
│     best-effort writes providers.default_provider into velune.toml
│     (_persist_default_provider, model.py:650-666)
│
├─ VeluneREPL._handle_prompt            repl.py:1249-1466
│  │
│  ├─ ProviderRegistry.get("anthropic")   providers/registry.py:303-335
│  │     _keyed_factory pulls key from keystore at instantiation
│  │     (registry.py:118-134); cache rebuilt if key_revision changed
│  │     → new AnthropicProvider instance, wrapped in RetryingProvider
│  │       (registry.py:328-332)
│  │
│  ├─ adapter.initialize()   adapters/anthropic.py:71-86
│  │     lazily creates ONE httpx.AsyncClient(timeout=300.0), reused
│  │     for the adapter instance's lifetime
│  │
│  ├─ RetryingProvider.stream(...)        providers/retrying.py:87-122
│  │     retries only if zero chunks have been yielded yet;
│  │     a semaphore caps concurrent calls to this provider
│  │     (max_concurrent_requests, default 4)
│  │
│  ├─ AnthropicProvider.stream(...)       adapters/anthropic.py:193-250
│  │     client.stream("POST", "/v1/messages", stream=True)
│  │     parses SSE `data: ` lines; content_block_start /
│  │     content_block_delta / message_delta events
│  │
│  └─ first `content_block_delta` text delta
│        → yielded as StreamChunk
│        → propagates through RetryingProvider → chat handler → REPL render
│
└─ FIRST STREAMED TOKEN APPEARS ON SCREEN

   (If this raises before any chunk is yielded — TurnProviderError,
   RateLimitError, ProviderConnectionError, ProviderAuthenticationError —
   repl.py:1371-1466 catches it, and if safe_to_retry_elsewhere is True
   [no tool executed yet this turn], walks _next_fallback_candidate()
   to the next untried provider/model and retries the whole turn there.)
```

**Key observation:** `/provider add` and "get a streamed token" are two
independently-triggered flows connected only by shared state
(`credentials.json` + `model_registry`), with no single command that proves
the full path end-to-end. This is called out explicitly in §3.

---

## 3. v1 architecture evaluation

Ordered roughly by severity/blast-radius, not by the order features were
listed above.

1. **Three provider metadata tables, out of sync.** `providers/catalog.py`
   (16 entries, feeds `/providers`), `cli/commands/providers.py::_PROVIDER_META`
   (16 entries, feeds `velune provider ...`), and `registry.py`'s hardcoded
   factory list (18 entries — includes `llamacpp`/`openai-compat`, present in
   neither metadata table). Any new provider must be added in up to three
   places by hand, and the two CLIs already disagree about which providers
   even exist.

2. **Two independent "add provider" implementations.** The REPL's
   `ProviderPalette._connect` and the Typer `add_provider()` duplicate the
   entire prompt → validate → save → discover sequence with different input
   widgets, and only the Typer path auto-sets the first-added provider as
   workspace default (`_maybe_set_first_default`). `/providers add anthropic`
   in the REPL and `velune provider add anthropic` on the command line behave
   differently for the same user intent.

3. **Two unaware writers of the same config key.** `_maybe_set_first_default`
   (Typer, on key-add) and `_persist_default_provider` (REPL, on
   model-activate) both write `providers.default_provider` into `velune.toml`
   from different triggers, neither aware the other exists — a race/last-writer-wins
   surface with no single owner.

4. **No real authentication concept — "login" is an alias for "add a key."**
   `/connect`/`/login` is literally `cmd_login → ProviderPalette.run("add ...")`.
   This works for static API keys but has no room to grow into OAuth/device-code
   flows, multi-account switching, or per-key scoping without a rewrite.

5. **Env-var keys are permanently unmanaged.** A key sourced from
   `ANTHROPIC_API_KEY` is reported as `KeyState.ENV` and never re-verified,
   never tracked for staleness, never subject to expiration detection — it is
   trusted forever with zero lifecycle visibility, silently different from
   every other key path.

6. **Staleness sweep may be orphaned.** `reverify_stale()` (24h TTL) exists
   and is exposed, but the actual scheduled call site that would trigger it
   automatically at REPL/session start was not located — it appears reachable
   only via explicit user action (`/providers test`). If true, keys silently
   accumulate `STALE` status with no automatic remediation, mirroring a
   pattern already seen elsewhere in this codebase (features built but never
   wired into the live path — see `MEMORY.md` history of "orphaned subsystem"
   fixes across cognition, memory, and knowledge-graph work).

7. **`ProviderHealthMonitor` may never be started.** The monitor class, its
   30s polling loop, and its manifest output all exist
   (`providers/health_monitor.py`), and `ProviderRouter` is wired to consume
   it — but its instantiation site (`providers/subsystems.py:21-27`)
   explicitly comments "don't auto-start," and no confirmed `.start()` call
   site was found. If nothing ever starts it, `ProviderRouter`'s
   health-aware routing degrades silently to "no health data," with no error
   raised anywhere to reveal the gap.

8. **Non-uniform error typing breaks retry/fallback decisions.**
   `adapters/anthropic.py` explicitly maps 401→`ProviderAuthenticationError`
   and 429→`RateLimitError(retry_after=...)`. `adapters/openai_compat.py` —
   which backs OpenAI, vLLM, LocalAI, and other OpenAI-compatible
   endpoints — raises only generic `InferenceError`/`ProviderConnectionError`
   regardless of status code. `RetryingProvider` explicitly excludes
   `ProviderAuthenticationError` from retry ("retrying a rejected key wastes
   three attempts on something retrying can never fix") — but a revoked key
   on any `openai_compat`-backed provider never raises that type, so it *is*
   retried three times fruitlessly, and the cross-provider fallback loop
   can't distinguish "bad key, try elsewhere immediately" from "transient,
   worth one more try here."

9. **No proactive rate limiting.** The only throttle is a per-provider
   concurrency semaphore (default 4 in flight) plus reactive `Retry-After`
   backoff after a 429 has already happened. Bursty call patterns (Council
   fan-out across multiple roles) can trip a provider's real rate limit
   before any client-side signal exists to prevent it.

10. **No circuit breaker.** A provider having an outage is retried with full
    exponential backoff on *every subsequent turn*, rather than being marked
    unavailable for a cooldown window after N consecutive failures. This is a
    real latency tax stacked on top of #7 (if health monitoring isn't
    running, there's also no independent signal to short-circuit this).

11. **Fallback logic is duplicated, not shared.** The REPL's chat-turn
    fallback loop (`repl.py:1371-1466`) and `ProviderRouter.get_ordered_candidates`
    (used by Council) implement conceptually the same "skip already-tried
    providers" idea via two separate code paths. A fix or improvement to one
    (e.g., adding circuit-breaker awareness) does not automatically apply to
    the other.

12. **Timeouts are hardcoded per call site, not configurable.** 300s for
    every adapter's inference client, 10s for cloud validation, 3s for local
    validation, 2s for health checks, 0.05s/0.5s for two different Ollama
    liveness probes — none of it surfaced in `ProvidersConfig`/`velune.toml`.
    A user running a large local model on modest hardware has no way to
    raise the ceiling (or lower it for faster-failing cloud calls) without
    editing source.

13. **No expiration *detection*, only reactive rejection.** A key transitions
    to `INVALID` only after a live call returns a rejecting status — there is
    no advance warning ("this key hasn't been reverified in 20 days") on any
    passive surface (status bar, `/doctor`), only the manual `/providers
    test` path.

14. **Local provider discovery is scattered.** Ollama alone has three
    separate mechanisms with three different timeout conventions (0.05s
    memoized TCP probe in `keystore.py`, 0.5s in the palette UI, 3s in
    validation, plus a filesystem manifest scan) that aren't unified behind
    one liveness/discovery interface — each caller re-derives its own notion
    of "is Ollama up."

15. **Weak-key fallback is under-communicated.** When no OS keyring and no
    passphrase are configured, credentials fall back to a machine-ID-derived
    key with only a one-time warning at generation time — no persistent
    visible indicator (status bar, `/doctor`) that the *current* store is
    using the weak path, which matters given this project's history of
    credential-adjacent CVEs (CodeQL backup-secrets fix, credential-fallback
    P1).

16. **Adding a key never proves inference actually works.** Validation is a
    cheap 1-token / models-list call — a subtly broken configuration (wrong
    `base_url` for an `openai_compat` endpoint, a proxy that mangles SSE, a
    model ID typo) passes `/provider add` cleanly and only surfaces on the
    user's first real prompt, disconnected in time and UI from the add flow
    that "succeeded."

---

## 4. Provider Management v2 — subsystem design

Eighteen subsystems. Several are net-new; several formalize and fix an
existing v1 module rather than replace it outright — called out per
subsystem. Complexity ratings are relative to each other, not absolute.

### 1. Credential Manager

| | |
|---|---|
| Purpose | Single authoritative owner of every credential's lifecycle: acquisition, storage handoff, state transitions (`MISSING → UNVERIFIED → VERIFIED → STALE → INVALID`), env-var precedence. Replaces the current split where `keystore.py` owns storage *and* lifecycle state with no single "manager" facade above it. |
| Inputs | Raw key material (from prompt, env var, or import), provider ID, an explicit `source` tag (`interactive`, `env`, `imported`, `oauth` — new, for future OAuth support). |
| Outputs | `CredentialRecord{provider_id, state, source, last_verified, last_error}`; emits `credential.changed` events on the existing event bus for any subscriber (health monitor, session context, diagnostics). |
| Workflow | One entry point (`CredentialManager.add(provider_id, key, source)`) used by **both** the REPL and the Typer CLI — collapses weaknesses #2/#3. Delegates encryption/persistence to Secure Key Storage, verification to the Validation Pipeline, and only Credential Manager writes `KeyState` transitions. |
| Failure Modes | Storage write fails mid-transition → record left in prior state, not half-updated (transactional wrapper around Secure Key Storage's atomic write). Concurrent add from REPL and CLI in two processes → last-write-wins at the storage layer, detected via revision check, surfaced as a conflict warning rather than silent overwrite. |
| Performance | In-memory cache of `CredentialRecord`s, invalidated only on explicit write or external file-mtime change — avoids re-decrypting the store on every read. |
| Security | No plaintext key ever leaves this subsystem's boundary; callers get `CredentialRecord` metadata, never the raw key, except the Connection Manager at the moment of client construction. |
| Complexity | **Medium** — mostly a facade/consolidation over existing `keystore.py` logic plus the fix for #2/#3. |

### 2. Secure Key Storage

| | |
|---|---|
| Purpose | Encrypted at-rest persistence of the credential store. Formalizes what `keystore.py`/`crypto.py` already do; no fundamental redesign needed, since AES-256-GCM + atomic write + backup is sound. |
| Inputs | Serialized `CredentialRecord` map from Credential Manager. |
| Outputs | Encrypted `credentials.json` bytes on disk; decrypted map back to Credential Manager on load. |
| Workflow | Unchanged from v1: JSON → AES-256-GCM encrypt → temp file → fsync → atomic rename → `.bak` retained. Adds a store-level schema version field so future migrations don't need format sniffing. |
| Failure Modes | Corrupt/undecryptable file → auto-restore from `.bak` (kept from v1); if `.bak` also fails, surface a clear "credentials unreadable, re-add your keys" path rather than a stack trace. |
| Performance | Negligible — small file, encrypted/decrypted synchronously on the rare write/load path only. |
| Security | Same guarantees as v1 (AES-256-GCM, 0600/ACL permissions, atomic writes preventing partial-write corruption). |
| Complexity | **Low** — retain, add schema version. |

### 3. OS Keychain Integration

| | |
|---|---|
| Purpose | Source the master encryption key from the OS-native secret store (Windows Credential Manager / macOS Keychain / Linux Secret Service) so the encryption key itself isn't sitting in a config file. |
| Inputs | None from the user directly; queries the OS keychain API for an existing entry or creates one. |
| Outputs | A 256-bit master key handed to Secure Key Storage; a `KeySourceTier` enum (`KEYRING`, `PASSPHRASE`, `MACHINE_FALLBACK`) reported outward. |
| Workflow | Same priority order as v1 (keyring → passphrase → machine-ID fallback), but the resulting tier becomes a **persistent, queryable status**, not a one-time console warning — surfaced by Provider Diagnostics and `/doctor` (fixes weakness #15). |
| Failure Modes | Keyring service unavailable (headless Linux without Secret Service running) → falls through to passphrase tier; both unavailable → machine-fallback tier with a persistent (not just first-run) low-severity indicator. |
| Performance | One keychain round-trip at process start, cached for the process lifetime. |
| Security | This *is* the security boundary for the whole subsystem — the weak-fallback tier must remain visible for the life of the session, not just at generation time, given this project's credential-CVE history. |
| Complexity | **Low** — mostly re-labeling v1's `crypto.py` tiering as a first-class, always-visible status rather than a one-time warning. |

### 4. Validation Pipeline

| | |
|---|---|
| Purpose | Prove a credential actually works against the live provider, and — new in v2 — optionally prove the *inference* path works too, not just auth (fixes weakness #16). |
| Inputs | Provider ID, key (or none for keyless local providers). |
| Outputs | `ValidationResult{status, latency_ms, checked_endpoint, smoke_stream_ok: bool | None}`. |
| Workflow | Stage 1 (unchanged from v1): cheap auth-only probe — `/models` list or 1-token completion, whichever the provider supports for free. Stage 2 (new, opt-in on add, always-available on demand): a real streamed smoke request against the cheapest available model, confirming SSE parsing and tool-call accumulation work end-to-end — this is what closes the `/provider add` → first-token gap identified in §2/§16. |
| Failure Modes | Stage 1 fail → `INVALID_KEY`/`NETWORK_ERROR`/etc. as in v1. Stage 2 fail *after* Stage 1 passed → new `AUTH_OK_INFERENCE_FAILED` status, distinguishing "your key is fine, but streaming/base_url/model config is broken" from a bad key — a diagnosis v1 cannot currently produce. |
| Performance | Stage 1 stays cheap (10s cloud / 3s local timeout, as in v1). Stage 2 is opt-in precisely because it costs a real (tiny) inference call — never run silently in the background. |
| Security | Same key-handling boundary as v1; smoke-stream requests use the smallest/cheapest model available per provider to bound cost. |
| Complexity | **Medium** — Stage 1 is a rename of `validation.py`; Stage 2 is new but reuses the Connection Manager's streaming path directly. |

### 5. Provider Health Monitoring

| | |
|---|---|
| Purpose | Continuous, actually-running background signal of each provider's live availability and latency — fixing weakness #7 (possibly-orphaned monitor) by making "started" a first-class, verified bootstrap step rather than an optional call site nobody confirmed. |
| Inputs | Registered providers with a key (or reachable local endpoint). |
| Outputs | `ProviderManifest{status, p50/p95 latency, consecutive_failures}` per provider, published on the event bus. |
| Workflow | Same 30s polling loop concept as v1's `ProviderHealthMonitor`, but its `.start()` is called from the same tiered bootstrap path documented in `architecture.md` (Tier 1 background warm-up), with a startup-time assertion/log line proving it actually started — so "is this running" is answerable by grepping a log line instead of reading five files. |
| Failure Modes | A provider polling check itself times out (2s) → counted toward `consecutive_failures`, feeding Circuit Breaker; monitor task itself crashing is caught and restarted rather than silently dying. |
| Performance | Unchanged 30s interval, 2s per-check timeout, rolling 5-sample latency window — this cadence was already reasonable in v1. |
| Security | Read-only health probes; never sends real user prompts. |
| Complexity | **Low** — the design is already right in v1; the fix is operational (verify it starts) not architectural. |

### 6. Model Discovery

| | |
|---|---|
| Purpose | Learn which models each provider currently offers, combining static fallback lists with live API queries. |
| Inputs | Verified/keyless providers; existing `discovery/*.py` backends. |
| Outputs | `ModelDescriptor` records registered into the model registry. |
| Workflow | Retains v1's `ModelDiscoveryScanner` coordinator concept largely as-is — it's a reasonable design (concurrent per-provider discoverers, gated on key validity). Adds a single scheduled refresh (e.g. once per session start, plus manual `/model discover`) instead of relying purely on manual triggers. |
| Failure Modes | A provider's discovery endpoint fails → falls back to the adapter's static list (as Anthropic already does) rather than leaving that provider with zero models. |
| Performance | Concurrent fan-out preserved from v1; results cached per session, not re-fetched every prompt. |
| Security | Read-only `/models`-class calls only. |
| Complexity | **Low** — mostly retained from v1's `discovery/scanner.py`. |

### 7. Capability Detection

| | |
|---|---|
| Purpose | Tag each discovered model with structured capability data (context window, tool-calling support, streaming-tool-call support, vision, reasoning tier) as first-class registry data, not per-adapter heuristics buried in discovery backends (e.g. `discovery/ollama.py::_classify_capabilities`). |
| Inputs | Raw model metadata from Model Discovery. |
| Outputs | `ModelCapabilities` struct attached to every `ModelDescriptor`. |
| Workflow | One shared classifier module consulted by every discovery backend, instead of each backend (Ollama today, others implicitly via static lists) reimplementing its own capability guesswork. |
| Failure Modes | Unknown model family → conservative default capabilities (assume no advanced features) rather than guessing optimistically and failing at call time. |
| Performance | Pure computation over already-fetched metadata — no extra network cost. |
| Security | N/A — metadata only. |
| Complexity | **Medium** — the hard part is unifying today's scattered per-provider heuristics into one shared classifier without losing provider-specific nuance (e.g. Anthropic's hand-set capability levels). |

### 8. Provider Registry

| | |
|---|---|
| Purpose | The single source of truth for "which providers exist, what they're called, whether they need a key, and how to build a live adapter for them" — collapsing v1's three-way split (`catalog.py`, `commands/providers.py::_PROVIDER_META`, `registry.py`'s factory list) into one. |
| Inputs | A static provider descriptor table (id, display name, key requirement, env var, factory reference) maintained in exactly one file. |
| Outputs | Live `ModelProvider` adapter instances (via Connection Manager); display metadata for both the REPL and the Typer CLI, sourced identically. |
| Workflow | Both `/providers` (REPL) and `velune provider` (Typer) read the same descriptor table — fixes weaknesses #1 and #2 structurally, since there is no longer a second table to drift out of sync. |
| Failure Modes | Unknown provider ID requested → `ProviderNotFoundError` (unchanged from v1's typed error hierarchy — that part was already correct). |
| Performance | Lazy factory imports retained from v1 (no reason to eagerly import 20 adapter modules at startup). |
| Security | No credential material lives here — purely descriptive + factory wiring. |
| Complexity | **Medium** — the fix is consolidation discipline, not new logic; the risk is regressions in the two current call sites that read the old tables. |

### 9. Connection Manager

| | |
|---|---|
| Purpose | Own the actual `httpx.AsyncClient` instances and their pooling/lifecycle, decoupled from adapter rebuilds — fixing weakness #9 (every key-revision bump today discards a working connection pool along with the adapter). |
| Inputs | Provider ID, resolved base URL, timeout policy (from the new per-provider timeout config, fixing weakness #12). |
| Outputs | A pooled `httpx.AsyncClient` handed to whichever adapter needs it. |
| Workflow | Adapters ask the Connection Manager for a client instead of constructing their own in `initialize()`. A key change invalidates only the *credential* used in request headers, not the underlying TCP/TLS connection pool, when the base URL is unchanged. |
| Failure Modes | Pool exhaustion under heavy concurrent use → surfaced as a distinct, named error rather than an opaque `httpx` timeout. |
| Performance | Avoids the cold-TLS-handshake cost v1 pays on every key rotation; this is the main perf win of v2's connection layer. |
| Security | Never logs headers/keys; pooled connections still respect per-provider TLS verification (unchanged from v1's `httpx` defaults). |
| Complexity | **Medium-High** — touches every adapter's `initialize()`, the riskiest single refactor in this design given 20 adapters to migrate. |

### 10. Retry Engine

| | |
|---|---|
| Purpose | One shared retry implementation for both ordinary chat turns and Council calls, replacing v1's two duplicated implementations (`RetryingProvider` and the ad hoc `repl.py` fallback loop) — fixes weakness #11. |
| Inputs | A typed exception (from the now-uniform error hierarchy, fixing weakness #8), the request's "has any output already been delivered" flag. |
| Outputs | Either a retried result or a typed decision to hand off to Circuit Breaker / Automatic Fallback. |
| Workflow | Retains v1's sound core rules (retry-after honored over blind backoff, never retry a stream after the first chunk, never retry `ProviderAuthenticationError`) but requires every adapter to raise the typed hierarchy consistently — closing the `openai_compat.py` gap identified in weakness #8. |
| Failure Modes | Repeated identical errors → existing `ErrorLoopDetector` concept retained to abort rather than loop forever. |
| Performance | Same backoff/jitter bounds as v1 (`RetryPolicy` defaults); the win here is correctness (fewer wasted retries against a dead key) rather than raw speed. |
| Security | N/A. |
| Complexity | **Medium** — logic mostly exists in v1's `core/retry.py`; the work is deleting the second implementation in `repl.py` and auditing all 20 adapters for uniform error typing. |

### 11. Circuit Breaker

| | |
|---|---|
| Purpose | Stop hammering a provider that's clearly down, instead of paying a full retry-and-backoff tax on every single turn — net-new, fixes weakness #10. |
| Inputs | Consecutive-failure counts from Provider Health Monitoring and from live inference failures. |
| Outputs | Per-provider state: `CLOSED` (normal), `OPEN` (skip immediately, don't even attempt), `HALF_OPEN` (allow one probe request after a cooldown). |
| Workflow | N consecutive failures (from either live calls or the background health monitor) trips `OPEN` for a cooldown window; one successful `HALF_OPEN` probe closes it again. Automatic Fallback consults breaker state before ever attempting a provider, not just after it fails. |
| Failure Modes | Flapping provider (intermittently healthy) → cooldown window with jitter to avoid synchronized thundering-herd retries across concurrent sessions. |
| Performance | This is a pure latency win under outage conditions — skips a doomed request+backoff cycle entirely once tripped. |
| Security | N/A. |
| Complexity | **Medium** — a small, well-understood state machine; the integration work (wiring it to both Health Monitoring and Retry Engine) is the bulk of the effort. |

### 12. Automatic Fallback

| | |
|---|---|
| Purpose | Move a chat turn to the next viable provider/model when the current one fails, without duplicating the "which provider is next" logic in two places (fixes weakness #11 alongside Retry Engine). |
| Inputs | `tried_provider_ids`, current Circuit Breaker states, the `safe_to_retry_elsewhere` flag (retained from v1's `TurnProviderError` — this distinction, "don't retry elsewhere if a tool already ran," is correct in v1 and should be kept unchanged). |
| Outputs | A candidate provider/model to retry the turn on, or an exhausted-fallback error surfaced to the user. |
| Workflow | One implementation consumed by both the REPL chat-turn path and Council's `get_ordered_candidates`, skipping any provider currently `OPEN` in the Circuit Breaker in addition to already-tried ones. |
| Failure Modes | All candidates exhausted or circuit-open → clear, single user-facing message naming which providers were tried and why each failed, rather than only the last error. |
| Performance | Unchanged from v1's candidate-walking approach; the win is correctness/consistency, not speed. |
| Security | N/A. |
| Complexity | **Medium** — mostly consolidation of two existing, roughly-correct implementations. |

### 13. Expiration Detection

| | |
|---|---|
| Purpose | Turn staleness from a silent, manually-checked state into an actively-surfaced one — fixes weaknesses #6 and #13. |
| Inputs | `last_verified` timestamps from Credential Manager; the 24h staleness TTL (retained from v1). |
| Outputs | A `credential.expiring_soon` / `credential.stale` event, surfaced in the status bar, `/doctor`, and Provider Diagnostics. |
| Workflow | A scheduled sweep (confirmed to actually run — closing the orphaned-`reverify_stale` question from weakness #6) reverifies `STALE` credentials in the background at session start and periodically thereafter, same 4-concurrent cap as v1's `reverify_stale`. |
| Failure Modes | Network error during a background reverify → leave state `STALE` (not `INVALID`), exactly as v1's `verifier.py` already does — that rejection-vs-network-error distinction was correct and is retained. |
| Performance | Bounded concurrency (4, as in v1) to avoid a burst of validation calls at every startup. |
| Security | Never escalates a background check's failure into deleting or invalidating a key based on a network blip — only explicit rejection statuses count. |
| Complexity | **Low-Medium** — v1 already has the right logic in `verifier.py`; the fix is scheduling + surfacing, not new logic. |

### 14. Local Provider Discovery

| | |
|---|---|
| Purpose | One unified interface for "is this local provider (Ollama, LM Studio, llama.cpp, vLLM, ...) reachable and what does it have," replacing v1's scattered probes with inconsistent timeouts (weakness #14). |
| Inputs | Configured local endpoints (default ports, or `OLLAMA_HOST`-style overrides). |
| Outputs | `LocalProviderStatus{reachable, models, servable_vs_on_disk}` — retains v1's useful Ollama distinction between "servable now" and "on disk but daemon not serving it." |
| Workflow | One liveness-probe implementation with one memoization TTL, shared by the palette UI, validation, and doctor checks — instead of three call sites each choosing their own timeout (0.05s / 0.5s / 3s in v1). |
| Failure Modes | Daemon down → clearly distinguished from "no models pulled," matching v1's existing filesystem-manifest fallback (a genuinely good v1 feature, retained as-is). |
| Performance | Single shared memoized probe reduces redundant socket connects across UI, validation, and doctor surfaces. |
| Security | Local-only network calls (loopback); no credential material involved. |
| Complexity | **Low-Medium** — consolidation of existing, working logic rather than a redesign. |

### 15. Provider Diagnostics

| | |
|---|---|
| Purpose | A single place to answer "what's the state of my providers right now" — key age, verification state, keychain tier, circuit-breaker state, recent latency — net-new as a unified surface (v1 has the data scattered across `doctor.py`, `provider_ui.py`, and `health_monitor.py` with no single view). |
| Inputs | Live reads from Credential Manager, Circuit Breaker, Provider Health Monitoring, OS Keychain Integration. |
| Outputs | A structured report consumable by both a REPL command and `/doctor` (see next subsystem). |
| Workflow | Pure read-side aggregation — no new probes of its own; it queries the other subsystems' already-maintained state. |
| Failure Modes | A subsystem's data temporarily unavailable (e.g. health monitor just restarted) → reported as "unknown," never fabricated. |
| Performance | Cheap — in-memory state reads only. |
| Security | Never displays raw key material, only state/metadata (verified/stale/invalid, keychain tier, age). |
| Complexity | **Low** — an aggregation layer over subsystems that already exist elsewhere in this design. |

### 16. `/doctor` Integration

| | |
|---|---|
| Purpose | Make Provider Diagnostics' output reachable from the existing `velune doctor` surface users already know, rather than inventing a parallel command users have to discover separately. |
| Inputs | Provider Diagnostics' aggregated report. |
| Outputs | New/extended `velune doctor providers` output: per-provider row with key state, keychain tier, circuit state, last-verified age, smoke-stream result if run. |
| Workflow | Extends v1's existing `doctor.py:_check_*` pattern rather than replacing it — those live-ping checks were already reasonable; this adds the *lifecycle* dimension (age, tier, breaker state) they currently lack. |
| Failure Modes | A single provider check failing must not abort the rest of the report — retains v1's per-check try/except isolation, which was already correct. |
| Performance | No change to `doctor`'s existing cadence/cost; diagnostics are additive fields on an existing report. |
| Security | Same display constraints as Provider Diagnostics (no raw keys). |
| Complexity | **Low** — additive to existing `doctor.py` structure. |

### 17. Session Provider Context

| | |
|---|---|
| Purpose | The single owner of "what's the active provider/model for this session," replacing v1's two unaware writers of the same `velune.toml` key (weakness #3). |
| Inputs | User actions (`/model use`, first-run default), persisted preference file. |
| Outputs | `repl.active_model`/provider equivalent, plus the one authoritative write path to `velune.toml`'s default-provider key. |
| Workflow | Both the REPL's `activate_model` path and the Typer CLI's first-add-sets-default behavior call into this one subsystem instead of each writing the config file directly — removing the race described in weakness #3 by construction (only one writer exists). |
| Failure Modes | Config write fails → in-memory session state still reflects the requested selection for the current process; failure to persist is surfaced, not silently dropped. |
| Performance | Negligible — a config write only on selection change, as in v1. |
| Security | N/A. |
| Complexity | **Low-Medium** — mostly deleting one of the two existing writers and routing both call sites through the survivor. |

### 18. Background Health Checks

| | |
|---|---|
| Purpose | The scheduling/orchestration layer that ties Provider Health Monitoring, Expiration Detection, and Circuit Breaker cooldown probes together into one background cadence, instead of three independently-scheduled (or, per weakness #7, possibly not scheduled at all) loops. |
| Inputs | Bootstrap-time registration from each of the three subsystems above. |
| Outputs | A single background task whose "is it running" state is itself verifiable (log line / diagnostics field), directly closing weakness #7. |
| Workflow | One `asyncio.Task` fans out to per-subsystem check functions on their respective intervals (30s health poll, on-demand expiration sweep, breaker half-open probes) rather than three ad hoc loops. |
| Failure Modes | The task itself crashing is caught and restarted with backoff, and surfaced in Provider Diagnostics as "background checks degraded" — rather than silently stopping as may be happening today. |
| Performance | Same total probe cadence as v1's separate loops, just coordinated under one supervisor. |
| Security | Read-only probes only, as in v1. |
| Complexity | **Medium** — the supervisor itself is simple; the value is entirely in making "is this actually running" a verifiable property. |

---

## 5. v1 vs v2 comparison

| Dimension | v1 | v2 |
|---|---|---|
| Provider metadata | 3 divergent tables (`catalog.py`, `_PROVIDER_META`, registry factory list) | 1 table, read by both REPL and CLI |
| "Add provider" flow | 2 separate implementations (REPL palette vs Typer command), different behavior | 1 implementation behind Credential Manager, used by both surfaces |
| Default-provider config write | 2 unaware writers of the same `velune.toml` key | 1 owner (Session Provider Context) |
| Auth model | "/login" = alias for "add key"; env-var keys unmanaged forever | Credential Manager tracks `source` explicitly; room for OAuth later; env-var keys still lifecycle-tracked as a distinct tier |
| Key validation | One cheap auth-only probe | Two-stage: auth probe (as v1) + optional smoke-stream proving real inference |
| Staleness handling | `reverify_stale()` exists; scheduled trigger unconfirmed | Expiration Detection with a confirmed, scheduled sweep + status-bar/doctor surfacing |
| Health monitoring | `ProviderHealthMonitor` built; `.start()` call site unconfirmed | Background Health Checks supervisor with a verifiable "running" signal |
| Error typing | Anthropic adapter typed correctly; `openai_compat.py` (OpenAI, vLLM, LocalAI, ...) untyped | Uniform typed hierarchy enforced across all adapters |
| Retry logic | `RetryingProvider` (chat) + separate `router.py` candidate logic (Council) — duplicated | One Retry Engine shared by both call paths |
| Circuit breaking | None — every turn retries a dead provider from scratch | Per-provider `CLOSED/OPEN/HALF_OPEN` breaker informed by health + live failures |
| Fallback | Chat-turn loop and Council routing implemented independently | One Automatic Fallback consulted by both, breaker-aware |
| Connection pooling | Per-adapter-instance pool, discarded on every key rotation | Connection Manager pool decoupled from credential rotation |
| Timeouts | Hardcoded per call site (300s/10s/3s/2s/0.05s/0.5s), no config | Per-provider configurable via `ProvidersConfig`/`velune.toml`, sane defaults retained |
| Rate limiting | Reactive only (Retry-After + concurrency semaphore) | Same reactive layer retained (proactive limiting judged unnecessary complexity — see roadmap note) |
| Local provider discovery | 3+ call sites, 3 different timeout conventions | 1 shared probe/interface, 1 memoization policy |
| Weak-key fallback visibility | One-time console warning | Persistent status surfaced in Diagnostics/`/doctor` |
| Diagnostics | Scattered across `doctor.py`, `provider_ui.py`, `health_monitor.py` | Single Provider Diagnostics aggregator, exposed via `/doctor` |
| End-to-end proof on add | None — add only proves auth, not streaming | Optional smoke-stream closes the gap between "key added" and "first token confirmed" |

---

## 6. Phased migration roadmap

Ordered so each phase is independently shippable and testable against the
real CLI (matching this project's established pattern of incremental,
test-verified delivery rather than big-bang rewrites), and so that later
phases build on consolidation done earlier rather than fighting three
divergent tables at once.

**Phase 0 — Consolidate duplicated sources of truth (no new features).**
Merge the three metadata tables into one (`Provider Registry`), unify the
two "add provider" implementations behind one code path (`Credential
Manager`), and resolve the two competing `velune.toml` default-provider
writers (`Session Provider Context`). This is pure de-duplication with no
behavior change beyond consistency — do it first because every later phase
would otherwise need to touch three places instead of one.

**Phase 1 — Credential Manager, Secure Key Storage, OS Keychain
Integration.** Mostly formalizing existing `keystore.py`/`crypto.py` logic
(already sound) behind the Phase 0 unified entry point; add the persistent
weak-key-tier visibility fix.

**Phase 2 — Validation Pipeline, Expiration Detection.** Add the
two-stage (auth + optional smoke-stream) validation and confirm/fix the
staleness-sweep scheduling gap.

**Phase 3 — Provider Registry, Connection Manager.** Land the single
provider descriptor table (depends on Phase 0's consolidation) and decouple
connection pooling from credential rotation; introduce per-provider
configurable timeouts.

**Phase 4 — Retry Engine, Circuit Breaker, Automatic Fallback.** Requires
first auditing and fixing all 20 adapters for uniform typed-error raising
(closing the `openai_compat.py` gap) — do this adapter audit as a prerequisite
sub-step before wiring the shared retry/fallback engine, since inconsistent
error types would otherwise make the new breaker/fallback logic behave
inconsistently by provider, silently reproducing the bug this phase exists
to fix.

**Phase 5 — Model Discovery, Capability Detection, Local Provider
Discovery.** Lower risk, mostly consolidating already-working v1 logic
(scanner concurrency, Ollama servable-vs-on-disk distinction) behind shared
interfaces.

**Phase 6 — Provider Health Monitoring, Background Health Checks, Session
Provider Context finalization.** Confirm/fix the health monitor's actual
startup wiring, land the unified background supervisor, and finish routing
all provider-selection writes through the single owner from Phase 0.

**Phase 7 — Provider Diagnostics, `/doctor` Integration.** Last, since it's
a pure read-side aggregator over every subsystem above — building it earlier
would mean repeatedly re-plumbing it as each underlying subsystem's shape
changes through Phases 1–6.

Each phase should ship with real-CLI smoke verification (this project's
established practice per its `verify` skill and memory record of catching a
CLI-hang bug via manual smoke test rather than unit tests alone) in addition
to unit coverage, particularly around Phase 4's retry/breaker/fallback
interaction, which is the highest-risk behavioral change in this roadmap.
