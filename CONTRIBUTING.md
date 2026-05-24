# ---
# title: "Contributing"
# description: "Guidelines for contributing to Velune."
# ---

# Contributing

Thanks for helping improve Velune.

## Local setup

```bash
pip install -e .[dev]
```

## Checks

Run the core checks before opening a pull request:

```bash
ruff check .
black --check .
python -m compileall velune
```

## Pull requests

Keep changes focused, keep generated artifacts out of the tree, and update the
README when user-facing commands or configuration change.

---
License: MIT
Copyright © 2026 Velune Contributors
