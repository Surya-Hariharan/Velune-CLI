# Project origin and provenance

This document records the development history of Velune CLI as it appears in
this repository's Git history, tags, and [CHANGELOG.md](../CHANGELOG.md). It
only states what those records support. Nothing in the history has been
rewritten or backdated; the commits and tags are the primary record.

## Origin

- **Original author and maintainer:** Surya HA
  ([@Surya-Hariharan](https://github.com/Surya-Hariharan)).
- **Canonical repository:** <https://github.com/Surya-Hariharan/Velune-CLI>
- **First commit:** `7a2ff33` ("Initial commit"), 2026-05-22. As of
  this writing, 289 of the 294 commits are authored by Surya HA; the rest are
  3 GitHub Copilot coding-agent commits and 2 Dependabot commits.
- **Public release files:** `0e48379` (2026-05-22) added CI, CONTRIBUTING,
  SECURITY, and CHANGELOG files.
- **License file:** the Apache-2.0 `LICENSE` first appears in commit `45c3974`
  (2026-06-20). Earlier commits do not contain a license file.
- **Package:** `velune-cli` on PyPI.

## Release history

Tag dates are Git tag creation dates. CHANGELOG headings carry their own
dates, which differ from the tag dates for a few early versions; both are
listed where they differ.

| Version | Tag date | Notes |
| --- | --- | --- |
| 0.1.0 | no tag | CHANGELOG entry dated 2026-06-05, described as the initial public release |
| 0.5.0-beta | 2026-06-10 (CHANGELOG: 06-07) | More providers, `/optimus`/`/godly` modes, project-type detection |
| 0.6.0 | 2026-06-12 | Provider health monitoring, health-aware routing, CI/CD pipeline |
| 0.9.0-beta | 2026-06-12 | Public beta |
| 0.9.0 | 2026-06-13 (CHANGELOG: 06-12) | Consolidated AST parsing and council orchestrators |
| 0.9.1 | 2026-06-21 (CHANGELOG: 06-14) | Stabilization and trust-recovery release |
| 0.9.2 | 2026-06-23 | Lean default install with opt-in extras |
| 0.9.3-beta.1 | 2026-06-23 | Instant startup, explicit on-demand cognition |
| 0.9.3 | 2026-06-24 | `/index` command rework |
| 0.9.3.1 to 0.9.3.5 | 2026-06-25 to 2026-06-27 | Patch releases |
| 0.9.4 | 2026-06-28 | Go launcher, Rust native module foundation, Repository Knowledge Graph |
| 0.9.5 | 2026-07-08 | Resource Connector Framework |
| 0.9.6 | 2026-07-18 | API-key lifecycle management, incremental repository cognition |
| 0.9.7 | 2026-07-31 | Terminal zoom-lock investigation, REPL and packaging fixes |

Commits after `v0.9.7` on `main` are unreleased (see the `[Unreleased]`
section of the CHANGELOG).

## Architecture and feature milestones

Dates below are from the CHANGELOG and the first Git commit that added the
relevant code.

- **0.1.0:** Typer CLI; LangGraph council orchestrator
  (Planner, Coder, Reviewer, Synthesizer); repository cognition with
  tree-sitter, BM25, and a vector store; Ollama, LM Studio, llama.cpp,
  OpenAI, Anthropic, and HuggingFace adapters; git-backed transactional
  execution with rollback.
- **0.5.0-beta:** additional cloud providers; session-wide `/optimus` and
  `/godly` modes; project-type auto-detection.
- **0.6.0:** provider health monitoring and a layered CI pipeline.
- **0.9.2:** lean core install with `[rag]`, `[parsing]`, `[telemetry]`,
  `[git]` extras.
- **0.9.3-beta.1 / 0.9.3:** instant-startup REPL; repository cognition
  became user-driven (`/index`).
- **0.9.4:** optional Go launcher and Rust native module
  (`ext/go` first added 2026-06-27, commit `2be547e`); Repository Knowledge
  Graph and Repository Intelligence Engine.
- **0.9.5:** `velune/resources` connector framework (first added 2026-07-08,
  commit `a245505`): Docker, PostgreSQL, MySQL/MariaDB, Supabase.
- **0.9.6:** key-verification lifecycle, incremental repository cognition,
  and architectural convergence of the memory and context layers.
- **0.9.7:** `velune doctor` terminal-capability reporting.

## Security milestones

- **0.1.0:** subprocess sandbox with write-path allowlists, cognitive
  firewall (prompt-injection detection), and secret scrubbing.
- **0.9.0-beta:** SSRF guard, `shell=True` blocking, and an architecture
  boundary check across the codebase.
- **0.9.1:** Windows PATH-hijack guard enforced; interpreter inline-code
  execution (`python -c`, `node -e`) blocked.
- **2026-07-18, `78f5e40`:** CodeQL clear-text-logging finding resolved.
- **2026-08-10, `9b6b6d3` and `4c56805`:** master passphrase file encrypted
  at rest; `cryptography` and `h2` dependency bumps.

Vulnerability reporting is described in [SECURITY.md](../SECURITY.md).

## How to verify

```bash
git log --reverse --format='%h %ad %an %s' --date=short   # full history
git tag --sort=creatordate --format='%(refname:short) %(creatordate:short)'
```

Releases are produced by the tag-triggered workflow in
`.github/workflows/release.yml`, which checks that the tag matches the
package version before publishing to PyPI and creating the GitHub Release.

Authorship, licensing, and governance: [AUTHORS.md](../AUTHORS.md),
[NOTICE](../NOTICE), [GOVERNANCE.md](../GOVERNANCE.md).
