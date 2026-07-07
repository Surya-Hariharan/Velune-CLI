# Velune CLI — v1.0 Readiness Audit (2026-07-07)

**Scope:** full-project audit against the bar set by Claude Code, Cursor CLI, GitHub
Copilot CLI, Gemini CLI, and Codex CLI. Velune is an *orchestration platform* over
external LLM providers; the audit judges it on that mission only.

**Snapshot:** v0.9.4 · 448 Python files · ~85,700 LOC across 27 subpackages ·
256 unit tests green (3 OS × 4 Python CI matrix) · reproducible builds ·
security gates (bandit, pip-audit, gitleaks, shell=True/asyncio.run regression checks).

---

## Verdict

Velune is **not yet a production-ready peer of Claude Code / Codex / Gemini CLI**,
but not for the reasons one would guess. The engineering hygiene — packaging,
startup latency, security posture, CI, recovery, cross-platform support — is
*already at or above* peer level after the 2026 hardening campaigns. What is
missing is the single capability that defines the product category:

> **The model cannot act.** In the primary chat path, the model can only emit
> text. It cannot read a file, search the repo, run a command, apply an edit,
> or call an MCP tool. Every peer CLI is, at its core, an agentic tool-use
> loop; Velune has built every component of that loop except the loop itself.

Everything below is ordered by how much it matters to closing that gap.

---

## Findings

### C-1 · No agentic tool-use loop in the primary chat path — **Critical**

- **Evidence:** `velune/cli/repl.py::_handle_prompt` (line ~884) builds an
  `InferenceRequest` from the conversation and streams text. There is no
  branch anywhere in the turn that can execute a tool and feed the result back
  to the model. The only edit path is the `/run` council pipeline
  (planner→coder→reviewer→challenger→synthesis), which is batch-oriented,
  multi-model, and not iterative.
- **Why it's a problem:** "AI coding CLI" today *means* an agent that
  autonomously reads, edits, runs, and iterates. Without it, Velune is a chat
  client with a separate code-generation batch command — a 2023-era shape.
- **User impact:** Users must manually paste context, and the model cannot
  verify its own work. The council can produce edits but cannot, e.g., run the
  tests it just broke.
- **Solution:** A native tool-calling loop: model emits `tool_calls`, Velune
  executes them via the existing `ToolRegistry`/`safety.ApprovalMode`
  machinery, appends results, repeats until a final text turn. All required
  primitives (tools with JSON schemas, permission enums, `guarded_execute`,
  sandbox, path guard, diff preview) already exist — only the loop and the
  wire-format plumbing are missing.
- **Complexity:** High overall, but cleanly phaseable (see roadmap).
  **Risk:** must be gated by the approval system from day one; a tool loop
  without permission gating is a security regression, not a feature.

### C-2 · Provider abstraction cannot represent tool calling — **Critical**

- **Evidence:** `velune/core/types/inference.py`: `InferenceRequest.messages:
  list[dict[str, str]]` (cannot even hold a tool-result message), no
  `tools`/`tool_choice` field; `InferenceResponse` has no `tool_calls`.
  No adapter (`openai.py`, `anthropic.py`, `ollama.py`, …) sends tools or
  parses tool calls. Meanwhile `ProviderCapabilities.supports_function_calling`
  is advertised `True` in several adapters — a capability flag with no
  implementation behind it.
- **Why:** This is the load-bearing wall for C-1. Every modern provider
  (OpenAI, Anthropic, Gemini, Groq, Ollama ≥0.3, vLLM, LM Studio) exposes
  native function calling; Velune's abstraction throws that capability away,
  directly contradicting the mission of "maximizing the capabilities of the
  connected models."
- **Solution:** Backward-compatible schema extension (`messages:
  list[dict[str, Any]]`, optional `tools`, `tool_choice`, `tool_calls`) +
  adapter support, starting with the OpenAI wire format as the internal
  normal form (OpenAI/Groq/OpenRouter/openai-compat/LM Studio/vLLM/Ollama all
  speak it natively; Anthropic needs a small translation shim that
  `_build_payload` is already positioned to do).
- **Complexity:** Medium. **Risk:** low — additive fields, existing callers
  unaffected.

### C-3 · MCP tools are unreachable by any model — **Critical**

- **Evidence:** `velune/mcp/` is a complete, tested client stack (stdio/SSE/
  HTTP/WS transports, registry, hot-reload, sampling, prompts). But
  `registry.call_tool` has **zero consumers outside the mcp package**; nothing
  in the CLI, council, or chat path ever surfaces MCP tools to a model.
- **Why:** MCP integration is a headline feature of every peer CLI because it
  lets *the model* use external tools. Today Velune's MCP support is transport
  plumbing without a purpose — the feature exists in the README but not in any
  user-reachable behavior.
- **Solution:** Once the tool loop exists (C-1/C-2), bridge
  `MCPServerRegistry.list_tools()` into the loop's tool schema list and route
  `tool_calls` with MCP-prefixed names through `registry.call_tool`.
- **Complexity:** Low once C-1/C-2 land — this is the payoff step.

### H-1 · Internal tool registry is likewise orphaned — **High**

`velune/tools/` (filesystem read/search/write, git, terminal, web) has schemas,
permission declarations, and `authorize_and_execute` — but its only consumer is
the *outbound* MCP server (`velune/mcp/server.py`, exposing Velune's tools to
other agents). Velune gives its tools to Claude Code but not to its own models.
Fix falls out of C-1.

### H-2 · Dual context systems; the sophisticated one is bypassed — **High**

- **Evidence:** `ContextOrchestrationEngine` implements budgeted, mode-aware,
  multi-source context assembly — but only the council path uses it. The chat
  path uses `conversation[-50:]`, a hardcoded `max_tokens=4096`
  (`repl.py:974`), and a 2-second best-effort semantic lookup.
- **Impact:** On a 200k-context model the user gets 4k output tokens and a
  50-message window; on a 4k local model, 50 messages can silently overflow
  the context and truncate the system prompt server-side. The flagship
  "repository cognition" work never reaches the conversation where users
  actually live.
- **Solution:** Route `_handle_prompt` context assembly through the existing
  budget engine (mode + model derived), and derive `max_tokens` from the model
  descriptor. **Complexity:** Medium. **Risk:** regressions in perceived
  latency; keep the 2s retrieval timeout behavior.

### H-3 · Test depth is thin relative to system size — **High**

256 unit tests across 26 files for 85.7k LOC (~335 LOC/test). Entire
subsystems — council orchestrator (1,300+ LOC), retrieval pipeline, context
assembler, execution DAG/rollback — have little or no direct coverage. The
suites that exist (keystore, recovery, MCP client, trust, redaction) are good;
coverage simply stops where the most complex code begins. Any refactor of the
council or retrieval layers is currently unverifiable.
**Solution:** contract tests with a `FakeProvider` for the orchestrator and the
new tool loop; golden tests for edit-format application. **Complexity:** Medium,
incremental.

### H-4 · Scope sprawl dilutes the mission — **High**

27 subpackages including `proactive/`, `intelligence/`, `knowledge/`,
`analysis/`, `daemon/`, three memory architectures ("three-brain", vitality
classification, lineage tiers), council-of-critics with per-critic configs,
blast-radius estimation, style resolvers, personality modules. The CLI package
alone is 24.8k LOC — larger than many entire peer CLIs. Each of these is
individually defensible; collectively they are why coverage is thin and why the
core loop never got built. **Solution:** freeze these surfaces (no new features
in them until the core loop ships), and fold or feature-flag the ones without
a user-visible command. **Not** proposing deletion in this pass — several are
load-bearing for existing commands. **Complexity:** organizational, low code
risk.

### M-1 · `datetime.utcnow()` deprecation — **Medium**

9 call sites in `velune/telemetry/usage_tracker.py`; emits DeprecationWarnings
today, breaks on a future CPython. Timestamps are compared lexically, so the
fix must preserve the naive-UTC ISO format
(`datetime.now(timezone.utc).replace(tzinfo=None)`), not switch to
offset-aware strings. **Complexity:** trivial.

### M-2 · Type checking is nominal — **Medium**

`pyproject.toml` disables ~15 pyright diagnostic categories
(`reportGeneralTypeIssues`, `reportCallIssue`, `reportArgumentType`, …).
The CI "Pyright type checking" gate therefore verifies little beyond syntax.
**Solution:** re-enable one category at a time, starting with
`reportCallIssue`, fixing as you go. **Complexity:** Medium, incremental.

### M-3 · `StreamChunk` cannot carry structured events — **Medium**

Text-only streaming means the future tool loop can't stream tool-call deltas or
emit typed progress events; the renderer will need a `metadata`-based or typed
event channel. Design it alongside C-2 so the wire types only change once.

### M-4 · No non-interactive/scripting mode — **Medium**

Peers all support `claude -p "…"` / piped-stdin one-shot invocations for CI and
scripting. Velune has subcommands but no headless prompt mode with `--json`
output. Straightforward to add once the tool loop exists (it is also the
natural test harness for the loop). **Complexity:** Low.

### L-1 · Line-ending hygiene — **Low**

`git diff` warns LF→CRLF on three files; no enforced `.gitattributes` policy
for `*.py`. One-line fix; prevents noisy cross-platform diffs.

### L-2 · `usage_tracker` and friends still on wall-clock string comparison — **Low**

Lexical ISO comparison works only while formats stay identical (see M-1);
long-term, store epoch seconds. Not urgent.

---

## What is already at peer level (do not touch)

- **Security:** argv-only sandbox with env scrubbing + process-tree kill, path
  guard, AES-GCM keystore, secret redaction in logs/traces, SSRF guard on MCP,
  backup encryption with passphrase-gated secret export, workspace trust
  prompts, threat model doc, four scanner gates in CI.
- **Packaging/startup:** lean core + graceful-degradation extras, lazy entry
  point (`--version` ~0.04s), reproducible builds, pure-py wheel check.
- **Reliability:** crash autosave + `/recover`, session archive, orphaned-task
  tracking, single owned event loop, provider health monitoring.
- **Cross-platform:** 3-OS × 4-Python CI, UTF-8 forcing, Windows DLL-failure
  UX, Ollama custom-drive discovery.

---

## Prioritized roadmap to v1.0

**P0 — the loop (v0.10):**
1. Commit pending hardening diff (keystore secret/metadata split, workspace
   recency `seq`, MCP test fixes). *(done this session)*
2. Extend inference types + adapters with native tool calling (OpenAI normal
   form; OpenAI/Groq/OpenRouter/openai-compat/Ollama first, Anthropic shim
   next). *(started this session)*
3. `ToolLoopRunner`: bounded infer→execute→append loop over `ToolRegistry`
   with `ApprovalMode` gating; `FakeProvider` contract tests. *(started this
   session)*
4. Wire into the REPL behind config (`agent.native_tools`), read-only tools
   auto-approved, write/exec tools prompt.
5. Bridge MCP registry tools into the loop (closes C-3).

**P1 — context + streaming (v0.11):**
6. Route chat context through `ContextOrchestrationEngine` budgets; derive
   `max_tokens` from the model descriptor (closes H-2).
7. Streaming tool-call deltas + tool-activity rendering in the stream renderer.
8. Non-interactive mode: `velune -p "…" [--json]` (closes M-4).

**P2 — trust the codebase (v0.12):**
9. Contract tests for council orchestrator + tool loop + edit formats (H-3).
10. Pyright: re-enable `reportCallIssue`, then `reportArgumentType` (M-2).
11. `utcnow()` fix (M-1) *(done this session)*; `.gitattributes` (L-1).

**P3 — polish and ship v1.0:**
12. Feature-freeze + fold speculative packages behind flags (H-4).
13. Docs pass: replace aspirational claims with the shipped loop; document the
    permission model.
14. Release engineering already in place — tag and publish.
