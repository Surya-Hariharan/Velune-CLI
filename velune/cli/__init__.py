"""CLI entry points.

``app`` is resolved lazily via ``__getattr__`` so that importing any
``velune.cli.*`` submodule (e.g. ``velune.cli.registry`` for fast top-level
help, or ``velune.cli.design`` for styling) does **not** pull in the entire
Typer application and its heavy transitive dependencies. Building the app at
package-import time used to cost ~1.9s on every ``velune.cli`` import.
"""

__all__ = ["app"]


def __getattr__(name: str):
    if name == "app":
        from velune.cli.main import app as _app

        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
