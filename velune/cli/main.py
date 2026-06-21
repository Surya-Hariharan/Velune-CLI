"""Backward-compatible CLI entry point."""

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from velune.cli.app import app

if __name__ == "__main__":
    app()
