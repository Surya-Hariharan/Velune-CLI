<div align="center">
  <img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/logo.png" alt="Velune Logo" width="100" />
  <h1>Security Policy</h1>
</div>

## Supported versions

Security fixes are triaged on `main`. Maintainers backport critical fixes to supported
release branches where feasible. Users should upgrade to the latest release for timely
protection.

| Branch / Release | Security support |
| :--- | :--- |
| `main` | ✅ Active — security fixes merged here first |
| Latest tagged release | ✅ Active — critical backports when feasible |
| Older releases | ⚠️ Best-effort; upgrade recommended |

---

## Reporting a vulnerability

> [!CAUTION]
> **Do not open a public GitHub issue for security vulnerabilities.**

### Primary channel

Use [GitHub Security Advisories](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new)
to open a private advisory. Maintainers can collaborate with you there before any
public disclosure.

### Encrypted fallback

If you cannot use GitHub Security Advisories, reach out to a maintainer through
their verified contact (`suryahariharan2006@gmail.com`) and request a secure channel. Do not post sensitive details
in public.

### What to include

```text
Title:              Short summary of the issue
Repository:         Surya-Hariharan/Velune-CLI
Affected versions:  <git SHA or package version>
Impact:             Brief note (RCE, data exposure, sandbox escape, etc.)
Reproduction:       Step-by-step or minimal PoC (redact sensitive data)
Environment:        OS, Python version, provider configuration
```

---

## Response timeline

| Severity | Definition | Acknowledgement | Remediation target |
| :--- | :--- | :--- | :--- |
| **Critical** | RCE, sandbox escape, data exfiltration | ≤ 24 hours | Emergency patch ASAP |
| **High** | Privilege escalation, significant data exposure | ≤ 72 hours | ≤ 30 days |
| **Medium** | Limited impact or requires user interaction | ≤ 5 business days | ≤ 90 days |
| **Low** | Informational, minor issues | ≤ 10 business days | Normal release cadence |

These are guidelines — exact timelines depend on complexity and external dependencies.

---

## Vulnerability lifecycle

1. Receive report via private advisory or encrypted channel.
2. Acknowledge and assign a triage owner.
3. Reproduce the issue; assign a severity rating (may consult reporter for PoC details).
4. Develop and test a patch on a private branch; add regression tests where applicable.
5. Coordinate disclosure: publish advisory, release notes, and CVE (if appropriate).
6. Close the advisory; notify the reporter; credit them in release notes.

Reporters are asked to allow a reasonable remediation window before public disclosure.
Reporters acting in good faith under responsible disclosure will not be pursued legally
for reasonable research activity; this is not a formal safe-harbor guarantee.

---

## Engineering safeguards

For the full attacker model, trust boundaries, and per-surface controls, see
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

### Managed command execution

Velune runs shell commands through `SubprocessSandbox`
(`velune/execution/sandbox.py`). To be precise about what this is: it is a **managed,
resource-limited execution environment**, **not** an OS-level security sandbox. There
is no namespace, seccomp, Job Object, or container isolation — commands run as the
invoking user. The controls below constrain *what* can run and bound its resources;
they do not isolate a process that is allowed to run. See
[docs/THREAT_MODEL.md › B1](docs/THREAT_MODEL.md) for the full residual-risk analysis.

- **No shell** — commands run via `subprocess.Popen(argv, shell=False)`; the argv is
  parsed with `shlex` and inline shell operators (`;`, `&&`, `|`, backticks, `$(...)`)
  are rejected even in argument position.
- **Executable allowlist** — only basenames on the configured allowlist may run, and
  the resolved binary must live in a trusted system/venv path (PATH-hijack guard,
  enforced on **both POSIX and Windows**). The validated absolute path is pinned to
  close the TOCTOU window before execution.
- **Interpreter inline-code blocking** — the allowlist includes interpreters
  (`python`, `node`, …). Inline-code flags (`python -c`, `node -e`/`--eval`/`-p`,
  including Python short-flag clusters like `-Ic`) are rejected so an agent cannot run
  arbitrary program text without first writing a file — and any agent-authored file
  must pass the `DiffPreview` write-approval flow before it can be run.
- **Environment scrubbing** — `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`,
  `PYTHONSTARTUP`, `BASH_ENV`, and similar injection vectors are stripped from the
  child environment.
- **Resource + lifetime limits** — wall-clock timeout and a memory ceiling, enforced
  by killing the **entire process tree** (`psutil`) so descendants never outlive a
  timeout or limit breach.

> **Known limitation.** Allowlisted interpreters and build tools (`python`, `node`,
> `go`, `make`, …) can execute files that exist in the workspace with your privileges.
> Treat command execution as "code you have allowed to run," not as a hard isolation
> boundary. True OS-level isolation is tracked as roadmap work in the threat model.

### Filesystem protection

- Every filesystem and git tool resolves paths through `PathGuard`
  (`velune/execution/path_guard.py`), which canonicalizes (resolving symlinks) and
  asserts the result stays within the workspace root — blocking `../` traversal,
  absolute-path escapes, and symlink escapes.
- All writes/creates/deletes go through a `DiffPreview` **approval flow**; nothing
  touches disk until the change is confirmed.

### SSRF and network hygiene

The web-fetch guard (`velune/tools/web/validator.py`) validates every outbound URL:

- Blocks loopback, RFC 1918 private ranges, link-local (`169.254.0.0/16`, `fe80::/10`),
  carrier-grade NAT (`100.64.0.0/10`), IPv6 ULA (`fc00::/7`), reserved, multicast, and
  the `0.0.0.0/8` range.
- Blocks cloud-instance **metadata endpoints** (AWS/Azure/GCP `169.254.169.254`, ECS
  `169.254.170.2`, GCP `metadata.google.internal`, Alibaba `100.100.100.200`, IPv6 IMDS).
- Resolves hostnames via `getaddrinfo` and checks **all** returned addresses, defeating
  DNS-rebinding and CNAME tricks; rejects numeric-escape IP forms (hex/octal/decimal),
  embedded credentials, and non-HTTP(S) schemes.

### MCP trust gating

External MCP servers are an outbound trust boundary. `velune/mcp/security.py`
validates every server URL before the client connects:

- Rejects embedded credentials and non-HTTP(S) schemes.
- **Always** blocks cloud-metadata and link-local targets (DNS-resolved, so rebinding
  cannot bypass it). Loopback/LAN remain permitted because local MCP servers are the
  common, legitimate case.
- Optional **host allowlist** (`[mcp] allowed_hosts` in `velune.toml`): when set, only
  listed hosts may be connected to — deny-by-default for everything else.

### Plugin trust model

Plugin sandboxing is **not yet implemented**. Plugins load in-process with full
process privileges, so discovery is **disabled by default** and unreachable from any
shipped CLI command; it requires an explicit opt-in
(`VELUNE_ENABLE_EXPERIMENTAL_PLUGINS=1` or `PluginLoader(experimental=True)`) and emits
loud warnings. Treat any plugin as arbitrary code you are choosing to run. Subprocess
isolation for plugins is tracked as future work.

### Secrets protection

- API keys live in the OS keyring (BYOK) with environment-variable fallback; they are
  never cached process-wide and never written into the workspace.
- **Log redaction** (`velune/core/redaction.py`): a logging filter scrubs known key
  shapes (`sk-…`, `sk-ant-…`, `xai-…`, `gsk_…`, `hf_…`, `AIza…`, OpenRouter, Fireworks,
  Replicate), `Authorization: Bearer/Token` headers, and the live values of configured
  provider env vars from every emitted log record and JSON exception trace.
- The `.veluneignore` template excludes `.env`, `*.pem`, `*.key`, and other credential
  files from indexing; runtime artifacts (`*.db-wal`, `*.db-shm`, `velune.local.toml`)
  are excluded from commits and indexing.

### Workspace / memory isolation

- Heavy per-project state (SQLite cognitive core, vector/semantic stores) is stored
  under `<app-data>/workspaces/<name>-<hash-of-absolute-path>/` (`velune/core/paths.py`),
  so two projects can never share a cognitive core, embeddings, retrieval index, or
  sessions. Same-named folders at different paths are disambiguated by a path hash.
- Live workspace switching shuts down old storage handles **before** rebinding services
  to the new root, so connections never leak and no cross-project memory bleed occurs.

### Transactional git-backed execution

- Plan-driven changes create a git stash or branch snapshot before any edit.
- On failure, the workspace is automatically restored to the pre-execution snapshot.

### Zero telemetry

No analytics, crash reports, or code snippets are transmitted to external servers.
All indexes and logs live exclusively in local, non-synced application storage.

---

## Credits

The project acknowledges security reporters in release notes and the advisory.
Monetary bounties are not currently offered; any bounty program will be announced
separately if introduced.

---

Apache License 2.0 — Copyright 2026 Surya HA
