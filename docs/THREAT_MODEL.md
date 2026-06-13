# Velune Threat Model

This document defines Velune's attacker model, trust boundaries, and the controls
enforced at each boundary. It complements [SECURITY.md](../SECURITY.md) (policy,
reporting) with the engineering rationale.

Velune is a **local-first** developer runtime. The design goal is that it behave like
a trustworthy tool a developer runs on their own machine — **not** an unsafe autonomous
shell wrapper. The central guarantee: untrusted inputs (LLM output, repository
contents, web/MCP responses) cannot escalate into arbitrary code execution, credential
theft, or cross-project data bleed without an explicit, visible user action.

## Assets

| Asset | Why it matters |
| :--- | :--- |
| Provider API keys (BYOK) | Financial + account compromise if exfiltrated |
| Source code in the workspace | Confidential; must not leak across projects or to the network |
| Per-project memory / embeddings / sessions | Cross-project bleed breaks confidentiality + correctness |
| The developer's machine | Subprocess execution must not become arbitrary RCE |
| Cloud-instance metadata (if run in CI/cloud) | SSRF target for credential theft |

## Trust levels

- **Trusted:** the user, `velune.toml` they author, the OS keyring.
- **Semi-trusted:** local MCP servers the user configured.
- **Untrusted:** LLM output (incl. proposed commands/edits), repository file contents,
  web responses, external/remote MCP tool output, plugin code.

## Trust boundary diagram

```
                            ┌─────────────────────────────┐
        UNTRUSTED  ───────► │   LLM output / model tokens │
   (prompt injection,       └──────────────┬──────────────┘
    poisoned repo files)                   │  proposed commands / edits / tool calls
                                           ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │                        VELUNE TRUST BOUNDARY                                │
   │                                                                             │
   │  command string ─► CommandSpec.from_string (shlex, reject shell operators)  │
   │                  ─► allowlist + PATH-hijack guard + pinned abs path         │
   │                  ─► SubprocessSandbox (shell=False, env-scrub, rlimits,     │
   │                                        process-tree kill)         ──────────┼──► child process
   │                                                                             │
   │  file path ──────► PathGuard.validate (canonical, symlink-resolved,         │
   │                                         within workspace) ─► DiffPreview ───┼──► disk (after approval)
   │                                                                             │
   │  web URL ────────► validate_url (SSRF: private/link-local/metadata,         │
   │                                  DNS-rebind, numeric forms) ────────────────┼──► network (allowed only)
   │                                                                             │
   │  MCP server URL ─► validate_mcp_url (metadata/link-local block,             │
   │                                      optional host allowlist) ──────────────┼──► MCP server (SSE)
   │                                                                             │
   │  secrets ────────► OS keyring (no process-wide cache)                       │
   │                  ─► SecretRedactingFilter on all log output                 │
   │                                                                             │
   │  per-project state ─► workspace_storage_dir = <app-data>/workspaces/        │
   │                       <name>-<sha1(abs path)>  (disjoint per project)       │
   └───────────────────────────────────────────────────────────────────────────┘
```

## Boundaries and controls

### B1 — Subprocess execution (untrusted command → host process)
**Threats:** RCE via shell injection, PATH hijacking, fork bombs, resource exhaustion,
orphaned descendants.
**Controls:** `shlex` parsing; rejection of shell operators; executable allowlist;
trusted-path/PATH-hijack guard with TOCTOU-pinned absolute path; `shell=False`;
environment scrubbing; wall-clock + memory limits; **process-tree** termination.
**Code:** `velune/execution/command_spec.py`, `velune/execution/sandbox.py`.
**Tests:** `tests/security/test_command_spec.py`, `tests/security/test_sandbox.py`.

### B2 — Filesystem access (untrusted path → disk)
**Threats:** path traversal (`../`), absolute-path escape, symlink escape, silent
overwrite.
**Controls:** `PathGuard` canonicalization + workspace containment; relative paths
anchored to the workspace (not process CWD); `DiffPreview` approval before any write.
**Code:** `velune/execution/path_guard.py`, `velune/tools/filesystem/*`.
**Tests:** `tests/security/test_path_guard.py`, `tests/test_filesystem_tools.py`.

### B3 — Outbound web (untrusted URL → network)
**Threats:** SSRF to cloud metadata / internal services, DNS rebinding, numeric-IP
obfuscation, credential-in-URL leakage.
**Controls:** `validate_url` — private/loopback/link-local/metadata blocking, all-address
DNS resolution check, numeric-form rejection, credential + scheme rejection.
**Code:** `velune/tools/web/validator.py`.

### B4 — MCP integration (external server → tool surface)
**Threats:** SSRF via a malicious/mistyped server URL; connecting to untrusted servers.
**Controls:** `validate_mcp_url` — always blocks metadata/link-local (DNS-resolved),
rejects credentials + non-HTTP(S); optional `[mcp] allowed_hosts` deny-by-default
allowlist. Loopback/LAN permitted for local-first MCP.
**Code:** `velune/mcp/security.py`, `velune/mcp/client.py`.
**Tests:** `tests/security/test_mcp_security.py`.

### B5 — Plugins (third-party code → process)
**Threats:** arbitrary code execution with full process privileges.
**Controls:** loading **disabled by default**, unreachable from shipped CLI, explicit
experimental opt-in with loud warnings. Sandboxing is acknowledged future work
(subprocess isolation), tracked as a known limitation.
**Code:** `velune/plugins/loader.py`.

### B6 — Secrets (keys → logs / disk / network)
**Threats:** key leakage via logs, tracebacks, or indexing.
**Controls:** OS keyring storage with no process-wide cache; `SecretRedactingFilter`
scrubs key shapes, bearer headers, and live env-var values from all log records and
JSON exception traces; `.veluneignore` excludes credential files.
**Code:** `velune/providers/keystore.py`, `velune/core/redaction.py`.
**Tests:** `tests/security/test_redaction.py`.

### B7 — Workspace / memory isolation (cross-project contamination)
**Threats:** one project reading another's memory, embeddings, retrieval index, or
sessions.
**Controls:** per-workspace storage keyed by a hash of the resolved absolute path;
distinct projects → disjoint storage dirs; workspace switch tears down old handles
before rebinding. **Rule: no cross-project contamination.**
**Code:** `velune/core/paths.py`, `velune/cli/workspaces.py`.
**Tests:** `tests/security/test_workspace_isolation.py`.

## Known limitations / future work

- **Plugin isolation:** no subprocess/permission sandbox yet — disabled by default.
- **Capability-based tool permissions:** tool access is currently gated by approval
  flows and allowlists rather than a formal per-tool capability grant model.
- **MCP tool-output trust:** responses from external MCP tools are treated as untrusted
  content but are not yet content-scanned for injection.

## Incident reporting workflow

1. **Report privately** via [GitHub Security Advisories](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new)
   (or an encrypted fallback channel). Do **not** open a public issue. See
   [SECURITY.md](../SECURITY.md) for the report template and severity/response SLAs.
2. **Triage:** a maintainer acknowledges, reproduces, and assigns severity
   (Critical/High/Medium/Low) per the SECURITY.md timeline.
3. **Remediate:** fix on a private branch **with a regression test** added under
   `tests/security/` that fails before the fix and passes after.
4. **Validate:** the fix must pass `ruff`, `pyright`, and the full `pytest` suite —
   including the new regression test — before merge.
5. **Disclose:** publish the advisory + release notes (and CVE if appropriate), then
   credit the reporter.
