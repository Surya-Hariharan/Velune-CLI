"""Public CLI entry point for Velune.

The console script (`velune`) targets :func:`main`. It deliberately avoids
importing the command graph or runtime at module load so that the most common
smoke command — ``velune --version`` — returns in milliseconds instead of
paying the full ~1.5s import cost of every subcommand module. The full Typer
application is built lazily only when an actual command is dispatched.
"""

from __future__ import annotations

import sys

# `app` is intentionally omitted: it is resolved lazily via __getattr__ for
# backward compatibility and is not a real module-level global.
__all__ = ["main"]


def main() -> None:
    """Console-script entry point.

    Fast-paths ``--version`` (the canonical install smoke test) without
    importing ``velune.cli`` or any subsystem, then delegates everything else
    to the lazily-built Typer application.
    """
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V") and "--help" not in argv:
        from velune import __version__

        if "--json" in argv:
            import json

            print(json.dumps({"version": __version__}))
        else:
            print(f"velune v{__version__}")
        raise SystemExit(0)

    from velune.cli.app import create_app

    create_app()()


def __getattr__(name: str):
    # Lazy attribute access keeps `from velune.main import app` working for
    # backward compatibility without building the app at import time.
    if name == "app":
        from velune.cli.app import app as _app

        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    main()
