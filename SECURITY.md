---
title: "Security Policy"
description: "Vulnerability reporting paths, secure disclosure policies, and core technical safeguards."
---

# 🛡️ Security Policy & Safeguards

Velune treats runtime security and code execution safety as fundamental kernel-level responsibilities. This document outlines our vulnerability disclosure process and details the layered systems engineering safeguards built into our execution core.

<div align="center">
  <!-- Concentric Security Safeguards -->
  <img src="assets/images/security-architecture.svg" alt="Velune Security Envelope" width="100%" style="border-radius: 6px; border: 1px solid #1e293b; background: #050811; padding: 10px; margin-bottom: 20px;" />
</div>

---

## 🏷️ Supported Versions

Security updates and patches are actively triaged and committed directly to the primary release branch.

| Branch / Release | Status | Security Support |
---
title: "Security Policy"
description: "Vulnerability reporting, triage, and runtime safeguards for Velune-CLI"
---

# 🛡️ Velune Security Policy

This document describes how to responsibly report security issues, how the
project triages and responds, and the core runtime safeguards that protect
users and contributors.

Scope
- Applies to the `Velune-CLI` repository and official packages published by
  the project.
- Includes: command-line runtime, sandbox execution, indexers, local
  provider integrations, and developer tooling shipped in repository.
- Excludes: third-party providers or cloud-hosted services unless the issue
  directly relates to repository code.

Reporting a vulnerability

Primary reporting channel
- Use GitHub's private Security Advisory flow on this repository: create a
  private advisory so maintainers and triage teams can coordinate securely.

Secondary (encrypted) channel
- If you cannot use GitHub Security Advisories, include an encrypted message
  to a maintainer's verified contact using the project's published PGP/GPG
  key (if available). Do NOT post sensitive details publicly.

What to include in a report
- A short summary of the issue and its impact.
- Exact reproduction steps or a minimal Proof-of-Concept (PoC). If a PoC
  includes sensitive data, provide a redacted version and offer to share
  full details securely.
- Affected versions (git SHA, package version) and environment details
  (OS, Python version, local model provider configuration).
- Relevant logs, tracebacks, command lines, and sample files.

Safe-testing guidance
- Only test against systems you own or have explicit permission to test.
- Avoid publishing exploit PoCs before the issue is fixed; provide them to
  maintainers privately to aid in triage.

Disclosure & coordination expectations
- Acknowledgement: maintainers will acknowledge receipt within 3 business
  days whenever possible.
- Initial triage: maintainers aim to provide a triage status and next steps
  within 5 business days.
- Patch timeline: the remediation timeline depends on severity (see
  Severity & SLA below). For critical vulnerabilities, maintainers may
  release emergency fixes or coordinate with downstream maintainers.

Severity & SLA
- Critical — remote code execution, sandbox escape, or data exfiltration:
  initial response within 24 hours; remediation and advisory as soon as
  possible (may be emergency patch).
- High — privilege escalation, significant data exposure, or persistent
  compromise: initial response within 48–72 hours; targeted patch within 30
  days or a coordinated mitigation plan.
- Medium — limited impact or requires user interaction: initial response
  within 5 business days; fix within 90 days or via a scheduled release.
- Low — informational, minor issues: response within 10 business days; fix
  in normal release cadence.

These SLAs are guidelines — exact timelines may vary based on complexity
and external dependencies.

Vulnerability lifecycle (maintainer view)
1. Receive report (private advisory or encrypted channel).
2. Acknowledge and assign a triage owner.
3. Reproduce and assign severity (may consult reporter for PoC details).
4. Develop and test a patch in a private branch; include tests where
   applicable.
5. Coordinate release and disclosure (publish advisory, release notes, and
   CVE if appropriate).
6. Close the advisory after release and notify the reporter.

Responsible disclosure and public timeline
- We request reporters allow a reasonable time for patching before public
  disclosure. Reporters who wish to publish publicly should coordinate with
  maintainers to avoid exposing users to unpatched exploits.

Credits and bounty
- The project maintains a public SECURITY.md to acknowledge reporters and
  may add credits to release notes. The project does not promise monetary
  bounties; potential bounties will be posted separately if/when available.

Engineering safeguards (summary)
- Subprocess sandboxing: runtime writes and shell commands execute inside a
  `SubprocessSandbox` with explicit allowlists and resource limits.
- Network hygiene: external fetches are gated by DNS/IP checks to mitigate
  SSRF or DNS rebinding attacks.
- Secrets protection: indexers and commit hooks exclude common credentials
  and runtime artifacts (e.g., `*.db-wal`, `velune.local.toml`).
- Transactional execution: plan-driven changes use git-backed snapshots to
  allow safe rollback on failures.

Supported versions and maintenance policy
- Security fixes are triaged on `main`; maintainers will backport critical
  fixes to supported release branches when feasible. Users should upgrade to
  the latest `main` or a patched release for timely fixes.

Legal, safe-harbor, and testing rules
- Do not assume permission to test infrastructure you do not own. Test in
  isolated, local environments.
- Reporters acting in good faith will not be pursued legally for reasonable
  research and disclosure activity; this is not a guarantee and is subject
  to applicable laws and project policies.

Contact template (suggested)
```
Title: Short summary of the issue
Repository: Surya-Hariharan/Velune-CLI
Affected versions: <git-sha or version>
Impact: Brief note (data exposure, RCE, etc.)
Reproduction steps / PoC: [redacted or full if safe]
Logs & environment: OS, Python, provider config
Preferred contact method: GitHub Security Advisory / PGP key
```

Acknowledgements
- We appreciate coordinated, responsible disclosure. Significant reports may
  be acknowledged in release notes or the project `SECURITY` acknowledgements.

---
License: MIT
Copyright © 2026 Velune Contributors
We practice responsible disclosure: please allow maintainers reasonable time to triage and remediate before public disclosure.

## Security Engineering & Defense Pillars

Velune implements multi-layered runtime controls to reduce attack surface and contain execution threats.

### SSRF Mitigation & DNS Validation

- Private IP suppression blocks local and RFC1918 ranges when resolving remote resources.
- Dynamic DNS inspection is performed before any outbound socket is created to mitigate DNS rebinding risks.

### Sandboxed Process Execution

- Subprocess executions are wrapped in `SubprocessSandbox` envelopes with explicit command whitelists, memory/time limits, and workspace write guards.

### Data Leakage & Secret Prevention

- Indexers ignore common runtime artifacts (e.g., `*.db-wal`, `*.sqlite-wal`, `*.db-shm`, `*.sqlite-shm`).
- Known local settings and credential files are excluded from indexing and commits by default.

## Contact and acknowledgement

Reports submitted via GitHub Security Advisory will receive an acknowledgement from the maintainers. If you require encrypted communication, indicate preferred contact methods in your advisory and maintainers will respond with a secure channel.
---
License: MIT
Copyright © 2026 Velune Contributors
