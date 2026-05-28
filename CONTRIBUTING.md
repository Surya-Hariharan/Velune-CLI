# Contributing to Velune

Welcome — and thank you for helping improve Velune. This document outlines
how to contribute, preferred workflows, and review expectations.

If you're new, start with the `Quick Start` section in `README.md` to set up
your environment.

## Table of contents

- How to contribute
- Development setup
- Tests & quality checks
- Pull request process
- PR checklist
- Review guidelines
- Branching & commits
- Reporting issues and security
- Code of conduct

## How to contribute

Contributions come in many forms: bug reports, documentation fixes,
tests, small bugfixes, and larger features. For anything larger than a
trivial change, open an issue first to discuss scope and design.

Small fixes: open a source-level PR against `main` with a clear title and
test coverage where applicable.

## Development setup

1. Create and activate a virtual environment:

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

2. Install the package in editable mode with development dependencies:

```bash
pip install -e .[dev]
```

3. Run the workspace initialization (optional):

```bash
velune workspace init
```

## Tests & quality checks

Run these checks locally before opening a PR:

```bash
ruff check .
black --check .
isort --check-only .
python -m compileall velune
pytest tests/unit -q
```

Run the full suite to validate integration changes:

```bash
pytest tests -q
```

## Pull request process

1. Fork or branch from `main` with a descriptive branch name (e.g.
	 `feature/cli-json-output` or `fix/sandbox-timeout`).
2. Keep commits focused and atomic — one logical change per commit.
3. Ensure tests pass and linters are clean.
4. Open a PR against `main` with a concise description and link to
	 any related issue(s).
5. Add reviewers and address review comments promptly.

## PR checklist

Before requesting review, ensure:

- [ ] The PR is focused and addresses a single concern.
- [ ] All new behavior is covered by tests (unit or integration).
- [ ] Linting and formatting checks pass locally.
- [ ] There are no generated artifacts or large binaries committed.
- [ ] The branch targets `main` and has been rebased to the latest `main`.
- [ ] Commit messages are clear and follow a readable style.

## Review guidelines

- Be respectful and constructive in code review. Explain rationale, not
	just requests.
- Prefer small, incremental changes to minimize risk.
- When reviewing, verify tests and run `pytest` locally if the change is
	non-trivial.

## Branching & commit messages

- Branch naming: `feature/`, `fix/`, `chore/`, `docs/` followed by a short
	description.
- Keep commits small and descriptive. Use imperative tense in commit
	messages (e.g., "Add JSON output to CLI"). Consider Conventional Commits
	for clearer changelogs.

## Reporting issues & security

- Open issues for bugs or feature requests via the GitHub issue tracker.
- For security issues, follow `SECURITY.md` (do not open public issues).

## Code of conduct

Please follow the project's code of conduct to maintain a friendly and
inclusive community. If the repository has a `CODE_OF_CONDUCT.md`, follow
that; otherwise use standard respectful collaboration norms.

## Templates and automation

- If provided, use repository issue and PR templates when creating new
	issues or PRs.
- CI checks run automatically on PRs — use the CI output to guide fixes.

## Need help?

If you're unsure where to start, open an issue with the label
`good-first-issue` or ask a maintainer to point you to a small task.

---
License: MIT
Copyright © 2026 Velune Contributors
`CODE_OF_CONDUCT.md` if present, and be constructive in reviews and comments.

---
License: MIT
Copyright © 2026 Velune Contributors
or follow standard respectful collaboration norms.

---
License: MIT
Copyright © 2026 Velune Contributors
