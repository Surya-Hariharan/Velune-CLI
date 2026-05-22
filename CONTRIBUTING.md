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
black .
pytest
```

## Pull requests

Keep changes focused, include tests when behavior changes, and update the README when user-facing commands or configuration change.