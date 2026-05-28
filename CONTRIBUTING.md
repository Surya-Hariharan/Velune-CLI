# Contributing to Velune

Thank you for contributing! This file summarizes the most important workflows
to get you productive quickly and make review easier for maintainers.

## Quick start (developer)

Create a virtual environment and install development dependencies:

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

## Checks & linters

Run these checks before creating a pull request:

```bash
ruff check .
black --check .
isort --check-only .
python -m compileall velune
```

## Tests

Run unit tests during development and full suite before submitting:

```bash
pytest tests/unit -q
pytest tests -q  # full suite
```

## Pull request checklist

Use this short checklist to prepare a clean PR:

- [ ] The PR is focused and addresses a single concern.
- [ ] New code includes tests (unit/integration) as appropriate.
- [ ] Linting and formatting checks pass locally.
- [ ] No generated artifacts or large binaries are included.
- [ ] Branch is up-to-date with `main` and rebased if necessary.
- [ ] Commit messages are clear and descriptive.

When changes affect user-facing commands or configuration, update `README.md`.

## PR templates and review

Small pull requests are easiest to review; if your change is large, open a
draft PR early and solicit feedback. Reviewers may request small follow-ups
to improve clarity, tests, or documentation.

## Code of conduct

We aim for an inclusive, respectful community. Follow the project's
`CODE_OF_CONDUCT.md` if present, and be constructive in reviews and comments.

---
License: MIT
Copyright © 2026 Velune Contributors
or follow standard respectful collaboration norms.

---
License: MIT
Copyright © 2026 Velune Contributors
