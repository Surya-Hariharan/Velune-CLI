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


def _fatal_environment_error(exc: BaseException) -> None:
    """Print an actionable message for a broken Python/runtime, then exit.

    Reached when a top-level import fails — almost always because the Python
    installation or one of its compiled dependency DLLs is missing or
    corrupted (e.g. ``ImportError: DLL load failed while importing _ctypes``
    on Windows after a Python upgrade/uninstall). We deliberately do *not*
    show a raw traceback: it confuses non-developers and buries the fix.

    Note: this cannot catch the ``velune.exe`` launcher failing to locate
    ``pythonXY.dll`` (Windows error 126) — that happens in the C launcher
    *before* any Python runs. The remedy for that case is the same and is
    printed here so it is discoverable via ``python -m velune``.
    """
    msg = (
        "Velune could not start because the Python installation appears to be "
        "missing or corrupted.\n\n"
        f"  Underlying error: {type(exc).__name__}: {exc}\n\n"
        "How to fix:\n"
        "  1. Reinstall Python 3.10+ from https://www.python.org/downloads/\n"
        '     (tick "Add python.exe to PATH" in the installer).\n'
        "  2. Reinstall Velune into that interpreter:\n"
        "       python -m pip install --force-reinstall velune-cli\n"
        "  3. If the 'velune' command itself is broken, run Velune via the\n"
        "     module form, which never depends on the generated launcher:\n"
        "       python -m velune --help\n"
    )
    # Use a bare stderr write so this path has zero further import dependencies.
    sys.stderr.write("\n" + msg + "\n")
    raise SystemExit(1)


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

    # Identify the first positional token (the subcommand, if any) so we can
    # import *only* what this invocation needs. Skip options and the values of
    # the known value-taking root options (``-w/--workspace``, ``-c/--config``)
    # so ``velune -w /some/path`` is correctly seen as the bare REPL, not a
    # subcommand. An unknown positional simply falls back to full registration.
    value_opts = {"-w", "--workspace", "-c", "--config"}
    subcommand = None
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in value_opts:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        subcommand = arg
        break
    help_requested = any(a in ("--help", "-h") for a in argv)

    # Top-level help (``velune --help`` / ``velune -h`` with no subcommand) is
    # rendered straight from the spec table — it imports no command modules and
    # is therefore near-instant.
    if help_requested and subcommand is None:
        try:
            from velune.cli.registry import render_root_help
        except ImportError as exc:
            _fatal_environment_error(exc)
        render_root_help()
        raise SystemExit(0)

    try:
        from velune.cli.app import create_app
    except ImportError as exc:
        # A failed *top-level* import means the interpreter or a compiled
        # dependency DLL is unusable. Surface an actionable message instead of
        # a cryptic traceback / Windows DLL popup. Real command-level errors
        # are raised later by Typer and are intentionally not caught here.
        _fatal_environment_error(exc)

    # No subcommand → the bare interactive REPL, which needs zero subcommand
    # modules imported. Otherwise register just the invoked command.
    create_app(register=subcommand)()


def __getattr__(name: str):
    # Lazy attribute access keeps `from velune.main import app` working for
    # backward compatibility without building the app at import time.
    if name == "app":
        from velune.cli.app import app as _app

        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    main()
