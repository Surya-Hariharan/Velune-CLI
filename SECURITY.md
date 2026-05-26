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
| :--- | :--- | :--- |
| `main` (Latest) | 🟢 Active | Supported (Full Patches) |
| Older Releases | 🔴 Deprecated | None (Please Upgrade) |

---

## 📬 Reporting a Vulnerability

If you discover a security issue or diagnostic vulnerability, **do not open a public GitHub issue**. Help us maintain platform integrity by following our private secure disclosure channel.

### 1. Secure Submission Paths
- Use **GitHub's private Security Advisory flow** directly on the repository.
- Alternatively, contact the core maintainers through a verified private channel (e.g., mail coordinates listed on the organizational profile).

### 2. Triage Information Checklist
To help our security response team validate and patch the issue quickly, please include:
- `[ ]` A concise summary of the vulnerability class (e.g., sandbox escape, SSRF bypass).
- `[ ]` The specific CLI command, parameter, or source path affected.
- `[ ]` A reproducible step-by-step proof of concept (PoC).
- `[ ]` Relevant execution traces, diagnostic logs, or environment context.

### 3. Safe Disclosure Timeline
We operate under standard responsible disclosure policies:
> [!IMPORTANT]
> We request that you allow the engineering team a **reasonable window of time** to investigate, develop, and distribute a secure patch before publishing vulnerability details or reproduction materials publicly.

---

## 📐 Security Engineering & Defense Pillars

Velune implements rigid, multi-layered boundary controls at runtime to contain execution threats and protect developer environments.

### Pillar 1: SSRF Mitigation & DNS Validation
All external retrieval tools (such as web search queries, documentation fetchers, and remote package indexers) must pass through a strict network validation gateway before connection streams are initiated:

- **Private IP Suppression:** Explicitly blocks all network connections targeting local loopbacks (`localhost`, `127.0.0.1`, `::1`) and private IPv4/IPv6 ranges specified under RFC 1918 subnets (e.g., `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
- **Dynamic DNS Inspection:** Resolves remote host domains dynamically, inspecting resolved IP lists prior to socket creation to prevent DNS rebinding attacks.

---

### Pillar 2: Sandboxed Process Execution
To minimize the impact of automated code operations and shell commands, the runtime isolates subshell tasks:

- **Bounded Execution Containers:** Shell executions and subprocess chains are strictly contained within `SubprocessSandbox` environments.
- **Envelope Guards:** Sandboxes restrict execution to explicit command whitelists, enforce precise memory boundaries, limit execution duration, and prevent unauthorized writes outside of defined workspace bounds.

---

### Pillar 3: Data Leakage & Secret Prevention
Velune enforces automatic filters to ensure sensitive credentials, tokens, and active database transaction logs are never stored in the shared index or committed to source control:

- **Transactional Log Exclusion:** Automatically ignores high-concurrency SQLite WAL (Write-Ahead Log) files and SHM (Shared Memory) index locks (`*.db-wal`, `*.sqlite-wal`, `*.db-shm`, `*.sqlite-shm`) generated during runtime indexing.
- **Secrets Scrubber:** Systematically scrubs and excludes developer credential files (`velune.local.toml`, `local_settings.toml`) from being captured by the repository indexer, keeping tokens secure.

---

License: MIT
Copyright © 2026 Velune Contributors
