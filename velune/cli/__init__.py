"""CLI entry points.

``app`` is resolved lazily via ``__getattr__`` so that importing any
``velune.cli.*`` submodule (e.g. ``velune.cli.registry`` for fast top-level
help, or ``velune.cli.design`` for styling) does **not** pull in the entire
Typer application and its heavy transitive dependencies. Building the app at
package-import time used to cost ~1.9s on every ``velune.cli`` import.

Note: ``velune.cli.app`` is itself a submodule of this package (``app.py``).
The moment anything imports that submodule — including the ``cli.main``
indirection below — Python auto-binds the attribute ``velune.cli.app`` on
*this* package to that module object, which permanently shadows this
``__getattr__`` for the exact name ``"app"``. In other words ``velune.cli.app``
(attribute access on the package) and ``from velune.cli import app`` are NOT
guaranteed to return the Typer instance once ``cli.app`` has been imported —
they can return the submodule instead. No real call site in this codebase
does either of those two things (every caller uses
``from velune.cli.app import app`` or ``from velune.cli.main import app``
directly, both of which are unaffected), so this is a dormant, pre-existing
sharp edge rather than an active bug — flagged here so nobody "fixes" it by
routing this accessor through ``cli.app`` directly, which makes it worse, not
better.
"""

import typing

if typing.TYPE_CHECKING:
    from velune.cli.main import app as app

__all__ = ["app"]


def __getattr__(name: str):
    if name == "app":
        from velune.cli.main import app as _app

        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
