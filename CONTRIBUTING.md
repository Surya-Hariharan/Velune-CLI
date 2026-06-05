# Contributing to Velune

Welcome — and thank you for helping improve Velune. This document explains how to
contribute, preferred workflows, and review expectations.

If you're new, read the [Quick start](README.md#quick-start) section in `README.md`
first to get your environment set up.

---

## Table of contents

- [How to contribute](#how-to-contribute)
- [Development setup](#development-setup)
- [Tests and quality checks](#tests-and-quality-checks)
- [Pull request process](#pull-request-process)
- [PR checklist](#pr-checklist)
- [Review guidelines](#review-guidelines)
- [Branching and commits](#branching-and-commits)
- [Reporting issues and security](#reporting-issues-and-security)
- [Code of conduct](#code-of-conduct)

---

## How to contribute

Contributions come in many forms: bug reports, documentation fixes, tests, small
bugfixes, and larger features. For anything larger than a trivial change, open an
issue first to discuss scope and design before writing code.

Small fixes: open a PR against `main` with a clear title and test coverage where
applicable.

---

## Development setup

1. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\Activate.ps1         # Windows PowerShell
```

2. Install the package in editable mode with all development dependencies:

```bash
pip install -e ".[dev]"
```

3. Initialize the workspace (optional but recommended):

```bash
velune workspace init
```

---

## Tests and quality checks

Run these locally before opening a PR:

```bash
# Lint
ruff check velune/

# Type check (informational, non-blocking)
mypy velune/ --ignore-missing-imports --no-error-summary

# Unit tests only (fast)
pytest tests/unit -q

# Full suite including integration tests
pytest tests/ -q
```

All checks must pass before a PR will be merged.

---

## Pull request process

1. Fork the repository or create a branch from `main` with a descriptive name
   (e.g. `feature/cli-json-output` or `fix/sandbox-timeout`).
2. Keep commits focused and atomic — one logical change per commit.
3. Ensure tests pass and linters are clean locally.
4. Open a PR against `main` with a concise description and a link to any related
   issue(s).
5. Fill in the PR template (checklist included).
6. Address review comments promptly.

---

## PR checklist

Before requesting review, confirm:

- [ ] The PR addresses a single focused concern.
- [ ] All new behaviour is covered by tests (unit or integration).
- [ ] `ruff check velune/` passes with no errors.
- [ ] `pytest tests/ -q` passes with no failures.
- [ ] No generated artefacts or large binaries are committed.
- [ ] The branch targets `main` and is rebased to the latest `main`.
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`.
- [ ] Commit messages are clear and use imperative tense.

---

## Review guidelines

- Be respectful and constructive. Explain the rationale behind requests, not just
  the request itself.
- Prefer small, incremental changes to minimise risk and review burden.
- When reviewing a non-trivial change, run `pytest` locally to verify correctness.
- Approve only when you are confident the change is correct and the checklist is met.

---

## Branching and commits

Branch naming convention:

| Prefix | When to use |
| :--- | :--- |
| `feature/` | New functionality |
| `fix/` | Bug fixes |
| `chore/` | Dependency bumps, tooling, CI |
| `docs/` | Documentation only |
| `refactor/` | Internal restructuring without behaviour change |

Commit message style: use imperative tense and keep the subject line under 72
characters. Consider [Conventional Commits](https://www.conventionalcommits.org/)
for clearer automated changelogs (e.g. `feat:`, `fix:`, `docs:`, `chore:`).

---

## Reporting issues and security

- **Bugs and feature requests** — open an issue via the GitHub issue tracker using
  the provided templates.
- **Security vulnerabilities** — follow [SECURITY.md](SECURITY.md). Do **not** open
  a public issue for security reports.

---

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating you agree to uphold a friendly and inclusive environment. Violations
may be reported to the maintainer.

---

Apache License 2.0 — Copyright 2026 Surya HA
