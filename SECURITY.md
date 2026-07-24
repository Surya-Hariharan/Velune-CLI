# Security Policy

## Contents

- [Supported versions](#supported-versions)
- [Reporting a vulnerability](#reporting-a-vulnerability)
- [Response timeline](#response-timeline)
- [Vulnerability lifecycle](#vulnerability-lifecycle)
- [Engineering safeguards](#engineering-safeguards)
- [Credits](#credits)

---

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

> **Caution:** Do not open a public GitHub issue for security vulnerabilities.

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

Velune CLI is local-first by design: the central guarantee is that untrusted input (LLM
output, repository contents, web/MCP responses) cannot escalate into arbitrary code
execution, credential theft, or cross-project data bleed without an explicit, visible
user action. The controls below are the per-surface implementation of that guarantee.

### Managed command execution

Velune CLI runs shell commands through `SubprocessSandbox`
(`velune/execution/sandbox.py`). To be precise about what this is: it is a **managed,
resource-limited execution environment**, **not** an OS-level security sandbox by
default. There is no namespace, seccomp, Job Object, or container isolation — commands
run as the invoking user. The controls below constrain *what* can run and bound its
resources; they do not isolate a process that is allowed to run. True OS-level
isolation is available as an opt-in — see
[Optional Docker sandbox](#optional-docker-sandbox) below.

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
> boundary — unless the Docker sandbox below is enabled.

### Optional Docker sandbox

For real OS-level isolation, `velune/execution/docker_sandbox.py` provides a
`DockerSandbox` that runs every command inside a per-session container with the
workspace mounted as a volume, so agent-executed code cannot affect the host beyond
that mount. It is **opt-in, not the default**:

- Requires the `[docker]` extra and a reachable Docker daemon; falls back to
  `SubprocessSandbox` automatically if Docker is unavailable.
- Enable for all agent execution by setting `execution.docker_sandbox = true` in
  `velune.toml`, or start/inspect it directly with `/sandbox docker` /
  `/sandbox status` in the REPL.
- Each session gets its own container (`velune-<random>`), stopped and removed at
  session end; state does not persist across sessions.

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

### Workspace / project trust

Separately from the URL-level checks above, `velune trust add|list|forget`
(`velune/core/trust.py`) gates whether a *directory's* project-level config is honored
at all. A trusted directory's `.mcp.json` / `velune.toml` may be loaded — which can
spawn local MCP server processes and override provider `base_url`s; an untrusted
directory falls back to user-level config only. Inside the REPL, `/approve
[safe|ask|block]` is a separate, session-scoped control over whether individual
tool/command calls need confirmation (`ask`, the default), run unattended when known
read-only (`safe`), or are rejected outright (`block`).

### Plugin trust model

Plugins are declarative (markdown commands, `SKILL.md` context injection, subprocess
lifecycle hooks via `velune.hooks.HookDispatcher`, and MCP server registration —
`velune/plugins/manager.py` + `velune/plugins/declarative/`). There is no in-process or
subprocess loader for arbitrary plugin Python code: a prior experimental code-plugin
loader (`PluginLoader`/`PluginSandbox`, disabled by default and never reachable from any
shipped CLI command) was removed rather than finished, since the declarative system
covers the same needs without running third-party code inside or alongside the CLI
process. Treat plugin hook scripts (subprocess lifecycle hooks) as arbitrary code you
are choosing to run.

### Secrets protection

- API keys live in the OS keyring (BYOK) with per-provider environment-variable
  fallback (e.g. `GROQ_API_KEY`); they are never cached process-wide and never written
  into the workspace.
- On disk, keys are AES-GCM encrypted (`velune/providers/crypto.py`) under a master
  key sourced with a strength-first fallback: an existing OS keyring entry, else a
  newly generated one stored in the keyring, else a key derived from
  `VELUNE_MASTER_PASSPHRASE` (PBKDF2-HMAC-SHA256, 390k iterations — for headless
  servers, Docker, CI), and only as a last resort a weak machine-derived key so
  credentials are never written in the clear. That last case logs a one-time warning
  urging you to set `VELUNE_MASTER_PASSPHRASE`.
- **Log redaction** (`velune/core/redaction.py`): a logging filter scrubs known key
  shapes (`sk-…`, `sk-ant-…`, `xai-…`, `gsk_…`, `hf_…`, `AIza…`, OpenRouter, Fireworks,
  Replicate), `Authorization: Bearer/Token` headers, and the live values of configured
  provider env vars from every emitted log record and JSON exception trace.
- The `.veluneignore` template excludes `.env`, `*.pem`, `*.key`, and other credential
  files from indexing; runtime artifacts (`*.db-wal`, `*.db-shm`, `velune.local.toml`)
  are excluded from commits and indexing.
- **Secret scanning**: `gitleaks` runs on every push/PR in CI (`.github/workflows/ci.yml`,
  scanning only the commits actually introduced, not full history) as the real gate.
  Contributors can additionally opt in to a local pre-push scan
  (`pre-commit install --hook-type pre-push`; see CONTRIBUTING.md) that catches a
  secret before it ever leaves their machine, instead of only after it's already
  on the remote.

### Backup and recovery secrets

`velune backup` (`velune/recovery/archive.py`) never writes API keys to an archive in
the clear:

- By default, provider keys are **excluded** from the backup entirely — the exported
  `providers.json` carries only masked (`"***"`) key fields.
- `velune backup --with-secrets` requires a passphrase and AES-GCM encrypts the real
  key values into a separate `providers.json.enc` payload, using a key derived solely
  from that passphrase (`encrypt_with_passphrase`) — independent of the OS-keyring key
  above, so the archive stays restorable on a machine with no keyring entry. The
  passphrase itself is never stored; it must be supplied again on `velune restore` to
  decrypt.

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

An opt-in, off-by-default local crash reporter (`telemetry.crash_reports_enabled`,
or `/crashreports on`) writes a redacted JSON snapshot of an unhandled crash —
exception, traceback, OS/Python/Velune versions; explicitly no local
variables and no prompt/conversation content — to `~/.velune/crash_reports/`
on the user's own machine. This does not transmit anything anywhere; there is
no server for it to send to. It exists purely so a user can attach a report
to a GitHub issue themselves if they choose to. See
`velune/cli/crash_reporter.py`.

### Dependency security

- `mcp>=1.28.1` is pinned in `pyproject.toml` specifically to floor out
  CVE-2026-59950 in earlier `mcp` releases.
- The former `[llamacpp]` extra was **permanently removed**: `llama-cpp-python` pulls
  in `diskcache ≤ 5.6.3`, which has an unpatched unsafe pickle-deserialization RCE
  risk with no fixed version available. See [Optional extras](README.md#optional-extras)
  in the README for the current extras list.
- `uv.lock` pins the exact resolved dependency graph (versions + hashes); CI's
  `Verify lockfile reproducibility` step (`uv lock --check`) fails the build
  if it drifts out of sync with `pyproject.toml`, so what CI audits and
  scans is guaranteed to be what actually gets installed.

### Static analysis (Bandit)

Every PR is gated on `bandit -c pyproject.toml -r velune/ --severity-level
medium --confidence-level medium` — any medium+ severity/confidence finding
fails the build. A full low-severity report also runs (for visibility) but
does not gate the build directly; instead, a separate regression-baseline
step fails the build if the low-severity count *grows* past a checked-in
baseline (183 as of writing).

This is a deliberate choice, not an oversight: the existing low-severity
findings are overwhelmingly broad `try/except: pass` patterns used
throughout the codebase as an intentional resilience strategy (a failed
best-effort background probe, cache write, or telemetry span must never
crash the interactive session) — triaging and rewriting all ~183 in one pass
isn't a security fix, it's a mass refactor with its own regression risk, and
blocking every unrelated PR on it would just train reviewers to bypass the
gate. The baseline instead makes sure the count only ever goes down or stays
flat, never grows unnoticed; raising it is only valid in the same PR that
introduces and justifies the new finding.

See `.github/workflows/ci.yml` (`security` job) for the exact commands.

---

## Credits

The project acknowledges security reporters in release notes and the advisory.
Monetary bounties are not currently offered; any bounty program will be announced
separately if introduced.

---

Apache License 2.0 — Copyright 2026 Surya HA
