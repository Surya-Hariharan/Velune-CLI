# Provider Management v2 — Principal Engineer Design Review

*An adversarial review of [PROVIDER_MANAGEMENT_V2.md](PROVIDER_MANAGEMENT_V2.md),
written to find weaknesses before implementation starts, not to bless the
design. Scope: production readiness for thousands of developers across
Windows/macOS/Linux, enterprise, offline, and cloud-native environments.
No redesign proposed here — critique only.*

A few claims below are grounded in the current codebase, not just the
design doc: no file-locking library (`FileLock`/`flock`/`portalocker`) exists
anywhere in `velune/`; no proxy/CA-bundle handling (`verify=`, `cert=`,
`CA_BUNDLE`) exists in `velune/providers/`; `telemetry/spans.py` has zero
provider-specific instrumentation despite `usage_tracker.py` and
`cost_estimator.py` already existing unused by this design;
`velune/cli/workspaces.py::switch_workspace` rebuilds only memory/repository
modules, confirming provider/credential state is not workspace-scoped today.

---

## 1. Hidden assumptions

- **Single-process, single-machine.** Nothing in v2 addresses cross-process
  consistency. No file locking exists anywhere in this codebase. Two
  terminal sessions can race on `credentials.json` writes; `key_revision`
  cache invalidation is in-memory per-process, so a key rotated in terminal
  A is invisible to terminal B's already-built adapter until B independently
  writes something — there's no file-watcher or IPC to propagate the change.
  Circuit Breaker and the concurrency semaphore are also per-process state:
  two terminals each believe they have their own "4 concurrent slots" against
  the same provider, and one terminal's `OPEN` breaker is invisible to the
  other.
- **Home-directory config is always reachable/writable.** Locked-down
  enterprise endpoint-protection can restrict or virtualize user-profile
  writes; roaming-profile environments may roam the encrypted file but not
  the OS-keyring-bound master key, producing an undecryptable store on a new
  machine with no story for recovery beyond "re-add your keys."
- **Connectivity is a single bit.** `InternetConnectivityChecker` (retained
  in v2) treats online/offline as one global TCP probe. Real corporate
  networks are *partially* reachable — provider A allowed, provider B
  blocked by a DLP proxy allow-list — and nothing in the design distinguishes
  per-destination reachability from global reachability, which directly
  undermines the "partial connectivity" and "corporate firewall" scenarios
  this review was asked to evaluate.
- **`httpx`'s default `trust_env` proxy behavior is sufficient.** No custom
  CA bundle, no MITM-inspection cert support, no proxy authentication
  (NTLM/Kerberos), no per-provider proxy override, anywhere in the design.
  A corporate TLS-inspecting proxy will produce an opaque SSL failure that
  the Validation Pipeline's stage-1/stage-2 probes will report as generic
  `NETWORK_ERROR` — no actionable "your proxy's CA isn't trusted" guidance.
- **One active provider per session is still the right mental model.**
  Session Provider Context fixes the *write race* from v1 but keeps the
  same single-active-provider shape. It doesn't model a conversation whose
  history genuinely spans two providers (mid-conversation fallback, or
  deliberate multi-model comparison) — a real correctness risk, since
  replaying one provider's tool-call history into a differently-shaped
  adapter isn't addressed anywhere.
- **All providers are SSE-shaped.** The entire adapter model — even in
  v2 — assumes `data:`-line SSE streaming. gRPC streaming, WebSocket duplex
  tool-calling, and long-poll/callback inference (all realistic for future
  or enterprise-internal endpoints) have no home in this design.
- **A credential is one static string.** `CredentialRecord` is still
  single-key shaped. Azure OpenAI (endpoint + api-version + key +
  deployment), AWS Bedrock (access key + secret + region + session token),
  and any OAuth/SSO flow (token + refresh token + expiry) don't fit.
- **The model catalog is small and provider-curated.** Fine for ~20
  providers today; not designed for org-provisioned fine-tuned model IDs
  (ephemeral, tenant-scoped, potentially thousands) that don't come from a
  public model list.

## 2. Missing capabilities

Relative to what Copilot Business/Enterprise, Cursor, and typical internal
developer-platform tooling already ship:

- **No enterprise policy layer.** No admin-controlled allow/deny list of
  providers, no org-enforced default model, no centrally-pushed
  config-as-code that overrides user prefs, no "disable local providers in
  managed environments" toggle.
- **No real SSO/OAuth/device-code flow.** The `source` tag on
  `CredentialRecord` gestures at "oauth (future)" but there's no token
  refresh, no expiry-driven re-auth, no browser/device-code flow designed —
  this is table stakes for enterprise adoption, not a nice-to-have.
- **No cost/usage accounting integration**, despite `telemetry/cost_estimator.py`
  and `usage_tracker.py` already existing in this codebase, unreferenced by
  the v2 design. No budget caps, no spend alerts, no per-user/per-provider
  breakdown.
- **No audit log** of credential lifecycle events (who added/rotated/removed
  a key, when) — `credential.changed` is an ephemeral event-bus emission,
  not a durable, tamper-evident record.
- **No key-rotation workflow.** Rotating a key is modeled as the same "add"
  operation as first-time setup — no grace period, no staged cutover, no tie
  to Expiration Detection's own staleness data.
- **No team-managed provider profiles**, despite this being named explicitly
  in the request this design responds to. Credential Manager is single-user,
  single-machine by construction.
- **No plugin/extensibility SDK for third-party adapters.** Provider
  Registry stays a static table you hand-edit — and notably, this codebase
  already has a working plugin/hook system for slash commands and MCP
  servers that this design doesn't reuse at all.
- **No durable, versioned capability manifest** (e.g., an exportable/
  validatable `provider.json`) — just in-memory structs.

## 3. Failure scenarios

| Scenario | v2 behavior as designed | Verdict |
|---|---|---|
| Provider outage | Circuit Breaker + Health Monitoring handle it *if* Background Health Checks is confirmed running; breaker state is per-process, so a second terminal has no shared knowledge of an open breaker | Improved but incomplete |
| Invalid/revoked key | Correct *only if* every one of 20 adapters was actually migrated off untyped errors in the Phase 4 audit; one missed adapter silently reproduces v1's fruitless-retry bug with no generic fallback heuristic to catch it | Fragile on audit completeness |
| Expired credentials | Blind 24h TTL doesn't reconcile with a future OAuth token's own expiry timestamp — two competing expiry concepts once OAuth exists | Not future-proofed |
| Slow providers | Per-provider configurable timeout, but static — Health Monitoring already collects p95 latency and nothing feeds it back into adaptive timeout tuning | Missed opportunity |
| Partial internet connectivity | Binary online/offline model can't represent "A reachable, B blocked" | Not modeled |
| Proxy / corporate firewall / VPN | No custom CA, no proxy auth, no per-provider proxy override; failures surface as opaque `NETWORK_ERROR` | Real gap |
| Local provider crash mid-stream | "Never retry after first chunk" silently truncates with the same generic error as a cloud disconnect — no distinct "local daemon died" message | UX gap, not just reliability |
| Concurrent requests | Bounded within one process (semaphore); unbounded across processes — two terminals double real load against a provider's actual rate limit | Not modeled across processes |
| Long-running streaming sessions | No discussion of idle-stream keep-alive, provider/proxy-side stream termination, or resume semantics | Not addressed |
| Multi-provider conversations | Session Provider Context is still single-active-provider; cross-provider tool-call history replay isn't addressed | Not addressed |
| Large workspaces | Out of scope for this doc, but not explicitly scoped out either | Ambiguous, should be stated |
| Multiple terminal sessions | The single biggest unaddressed gap — see §1 | Not addressed |
| Workspace switching | `switch_workspace` (confirmed in code) never touches provider/credential state; v2 doesn't say whether that's intentional or an oversight | Not addressed |

## 4. Security review

- **Storage/encryption fundamentals are sound and correctly retained**
  (AES-256-GCM, atomic write, `.bak` restore, 0600/ACL). No complaint here.
- **Process memory hygiene is unaddressed.** Decrypted keys sit as ordinary
  Python `str` objects — not zeroed, not `mlock`'d, no `SecretStr`-style
  wrapper — for the adapter's lifetime, with core-dump/swap exposure as a
  realistic risk the doc never raises despite this review asking about it
  explicitly.
- **No crypto algorithm agility.** No versioned scheme field; if AES-GCM
  ever needs replacing (e.g., a nonce-reuse finding), there's no migration
  path designed, only a data schema version for the *record*, not the
  *cipher*.
- **Key rotation isn't designed** (see §2).
- **Logging redaction isn't required by the design.** This codebase already
  fixed secret-log-redaction once (per project history), but the v2 doc
  never states that new Retry Engine/Diagnostics logging must reuse that
  existing redaction path — a real integration risk where a new log line
  added during implementation quietly bypasses it.
- **Clipboard exposure isn't addressed** for pasted keys, despite this
  review asking about it — no connection made to the codebase's existing
  OSC-52 clipboard work.
- **Subprocess environment leakage isn't addressed.** Velune shells out to
  git/tools elsewhere in the codebase; nothing in this design says whether
  provider API key env vars are scrubbed from a child process's inherited
  environment.
- **Backup/restore portability isn't re-examined.** A backup encrypted with
  a keyring-sourced master key is not restorable on a machine without that
  same keyring entry — the v2 doc doesn't revisit this real portability
  failure mode inherited from v1.
- **No explicit threat model.** "Security Considerations" per subsystem are
  one-line notes, not a stated adversary model — a co-located malicious
  process, a stolen laptop, a compromised dependency, and a malicious MCP
  server all imply different mitigations, and the doc never says which it's
  defending against.

## 5. Scalability review

- **100 providers:** the data structure scales; the *workflow* (hand-edit a
  static table) doesn't — no generated/pluggable registration path.
- **1000 models:** "discover everything at session start" isn't budgeted
  against a 1000+-model aggregator catalog (OpenRouter/Together/Fireworks-scale)
  — no incremental/paginated/lazy discovery discussed.
- **Enterprise deployments:** blocked primarily by the missing policy layer
  (§2), not by raw scale.
- **Multiple workspaces:** provider state isn't workspace-scoped, and the
  doc doesn't say whether it should be, despite `switch_workspace` being a
  real, existing feature this design silently ignores.
- **Multiple users / team-managed profiles:** no multi-user sharing model
  exists at all — Credential Manager is single-user by construction.

## 6. API design review

- **Registry:** reasonable consolidation of three tables into one, but still
  a static table + factory — not a formal, implementable plugin interface.
- **Manifests:** `ProviderManifest`, `CredentialRecord`, `ModelDescriptor`,
  `ModelCapabilities` are separately-owned ad hoc dataclasses with no shared
  schema-versioning convention (only Secure Key Storage gets a version
  field) and no stated backward-compatibility story for existing serialized
  v1 records.
- **Provider interface:** `ModelProvider.stream()` stays SSE-shaped (see §1)
  — v2 improves reliability around it without making it transport-agnostic.
- **Adapter abstraction:** v2 explicitly keeps "20 hand-rolled SSE parsers"
  as accepted rather than introducing a shared base class — a bigger
  dedup opportunity than several of the 18 chosen subsystems, and notably
  absent from the list.
- **Dependency inversion:** Connection Manager's "adapters ask for a client"
  contract is underspecified — constructor injection vs. service locator vs.
  context manager isn't decided, leaving room for 20 adapters to each
  interpret it differently.
- **Extensibility/plugins:** doesn't reuse the CLI's own existing
  plugin/hook system (already solved for slash commands and MCP servers) —
  providers remain a special-cased, hardcoded subsystem, which is an
  architectural inconsistency worth naming directly.

## 7. UX review

- **`/provider` / `/connect`:** merges the code path but ducks the actual
  product decision — does the unified flow keep the REPL's nicer palette
  UX, or regress to the Typer CLI's plainer prompt? The doc stays at the
  abstraction level and never says.
- **`/doctor`:** additive design risks an unreadably long report once
  20+ providers each grow key-age/breaker-state/keychain-tier fields with no
  summarized/verbose-toggle UX discussed.
- **Setup wizard:** not mentioned once. `onboarding/stages.py`'s
  `_configure_one_provider_key` is a real, separate first-run implementation
  per the v1 reverse-engineering — Phase 0's "unify the two add-flow
  implementations" only names the REPL and Typer CLI, silently leaving a
  *third* implementation (onboarding) unaddressed.
- **Error messages:** the new `AUTH_OK_INFERENCE_FAILED` status is a good
  diagnostic distinction, but no actual message copy or next-step guidance
  is specified — the user still has to debug base_url/model-id/proxy issues
  alone.
- **First-run experience:** effectively unaddressed given the setup-wizard
  gap above, despite being the highest-leverage UX moment for a new user.

## 8. Technical debt

- **Remaining duplication:** the onboarding wizard as a likely third
  "add credential" implementation; 20 hand-rolled SSE parsers left as-is.
- **Tight coupling:** Retry Engine, Circuit Breaker, and Automatic Fallback
  must agree on state transitions in the request's hot path — this is
  real synchronous coupling the phase-by-phase framing understates; in
  practice these three ship as one unit or not at all, and the roadmap's
  "Phase 4" undersells how large and hard-to-partially-ship that unit is.
- **Over-engineering risk:** 18 named subsystems for what's structurally a
  credential store + HTTP client wrapper + retry policy + small state
  machine. Provider Diagnostics and `/doctor` Integration are, by the doc's
  own description, thin aggregation layers — they probably don't warrant
  status as independent architectural subsystems.
- **Under-engineering risk:** conversely, the genuinely hard problems —
  cross-process consistency, multi-field/OAuth credentials, workspace
  scoping — get *no* subsystem at all. The 18-item list creates an
  appearance of thoroughness that doesn't line up with where the real
  difficulty actually is.
- **Future maintenance risk:** every new streaming-protocol wrinkle (e.g., a
  new tool-call wire format) is still a 20-file change under the current
  per-adapter pattern, compounding with every provider added going forward.

## 9. Missing observability

- No OpenTelemetry/structured-metrics export, despite `telemetry/spans.py`
  already existing with zero provider instrumentation in it today.
- No per-provider request-rate/error-rate/latency-percentile metrics
  exposed to a human or dashboard — Health Monitoring's rolling window is
  for internal routing decisions only.
- No trace/correlation ID threading a single chat turn across
  "tried provider A (failed) → fell back to provider B" — Automatic
  Fallback's user-facing message is ephemeral console text, not a durable,
  reconstructable trace.
- No hook into the already-existing `cost_estimator.py`.
- No structured "why did the breaker open" event with enough detail
  (failure types, timestamps) to debug a false-positive trip after the
  fact.

## 10. Future-proofing

- **MCP-native providers:** unaddressed — no cross-reference between this
  design and the codebase's existing, real MCP subsystem, despite the
  obvious conceptual overlap (an MCP server exposing a model-like
  interface).
- **OAuth / token auth / enterprise SSO:** the single largest gap. Only a
  placeholder `source` tag exists; no refresh lifecycle, no redirect/device
  flow. Retrofitting this later touches nearly every subsystem in the
  design (Credential Manager, Validation Pipeline, Expiration Detection,
  Connection Manager) — this should have been designed in from the start
  given how central it is to enterprise adoption.
- **Fine-tuned models:** no design for org-provisioned, non-catalog model
  IDs.
- **Self-hosted inference servers:** reasonably covered for "another
  OpenAI-compatible server"; not covered for bespoke auth (mTLS,
  service-mesh identity).
- **Multi-modal models:** not mentioned beyond a "vision" capability flag —
  no design for how image/audio input actually flows through the
  Connection Manager/adapter boundary, which is a current-generation
  requirement, not a speculative one.
- **Future provider APIs generally:** the hand-rolled-adapter-per-provider
  pattern is preserved, not reduced — v2 improves reliability, not the
  marginal cost of provider #21.

---

## Scores (1–10)

| Dimension | Score | Why |
|---|---|---|
| Architecture | 6 | Real consolidation of genuine v1 duplication; but conflates thin aggregation layers with subsystems, keeps the SSE-locked adapter pattern, doesn't reuse the CLI's existing plugin system. |
| Security | 5 | Storage/encryption fundamentals sound and retained; but no threat model, no memory hygiene, no rotation workflow, no audit trail, no corporate-proxy/CA story. |
| Scalability | 4 | Fine at today's ~20-provider scale; the review's own named targets — 100 providers, enterprise, multi-workspace, multi-user, team profiles — are almost entirely unaddressed. |
| Reliability | 6 | Circuit breaker/unified retry/fallback are real improvements over v1; but every improvement is per-process, and cross-process state is completely unaddressed despite "multiple terminal sessions" being a normal usage pattern. |
| Extensibility | 4 | Static table + factory, no plugin interface, no reuse of the existing hook/plugin system elsewhere in this CLI; adding provider #21 is still a manual multi-file edit. |
| Maintainability | 6 | Meaningfully better than v1 (kills the 3-way and 2-way duplication); but introduces subsystem-count ceremony while leaving 20 duplicated SSE parsers untouched. |
| Developer Experience | 6 | Smoke-stream validation and unified diagnostics are genuine wins; but onboarding-wizard integration, doctor-output density, and REPL-vs-Typer UX direction are all left unresolved. |
| Production Readiness | 4 | For the stated target — thousands of developers across enterprise/offline/cloud-native — the missing policy layer, SSO/OAuth, audit logging, proxy/CA support, and cross-process consistency are launch-blocking for a meaningful share of that audience, even though the design is a solid win for the single-user/single-session case. |

**Bottom line:** this is a good v1.5 — it fixes real, cited bugs and
duplication in the current codebase. It is not yet an enterprise-grade v2.
The largest single gap is that nothing in this design assumes more than one
process or more than one user is ever touching provider state at the same
time, which contradicts the review's own stated deployment target.
