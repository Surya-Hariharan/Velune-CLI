# Security Policy

## Supported versions

Security fixes are triaged on `main`. Maintainers backport critical fixes to supported
release branches where feasible. Users should upgrade to the latest release for timely
protection.

| Branch / Release | Security support |
| :--- | :--- |
| `main` | Active — security fixes merged here first |
| Latest tagged release | Active — critical backports when feasible |
| Older releases | Best-effort; upgrade recommended |

---

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

### Primary channel

Use [GitHub Security Advisories](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new)
to open a private advisory. Maintainers can collaborate with you there before any
public disclosure.

### Encrypted fallback

If you cannot use GitHub Security Advisories, reach out to a maintainer through
their verified contact and request a secure channel. Do not post sensitive details
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

### Subprocess sandboxing

All runtime writes and shell commands execute inside `SubprocessSandbox` envelopes with:

- Explicit command allowlists — only whitelisted executables may be invoked.
- Workspace write guards — file writes are restricted to the project root.
- Time and memory limits — runaway processes are terminated automatically.

### SSRF and network hygiene

- DNS resolution filters RFC 1918 private IP blocks (`10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`) and loopback (`127.0.0.0/8`, `::1`) before any outbound socket
  is created.
- Dynamic DNS inspection mitigates DNS rebinding attacks.

### Secrets protection

- The `.veluneignore` template excludes `.env`, `*.pem`, `*.key`, and other common
  credential files from indexing by default.
- Runtime artifacts (`*.db-wal`, `*.db-shm`, `velune.local.toml`) are excluded from
  commits and indexing.

### Transactional git-backed execution

- Plan-driven changes create a git stash or branch snapshot before any edit.
- On failure, the workspace is automatically restored to the pre-execution snapshot.

### Zero telemetry

No analytics, crash reports, or code snippets are transmitted to external servers.
All indexes and logs live exclusively in the local `.velune/` directory.

---

## Credits

The project acknowledges security reporters in release notes and the advisory.
Monetary bounties are not currently offered; any bounty program will be announced
separately if introduced.

---

Apache License 2.0 — Copyright 2026 Surya HA
