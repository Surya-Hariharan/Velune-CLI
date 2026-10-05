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


def _failing_dependency(exc: BaseException) -> str | None:
    """Return the third-party distribution an import failure points at, if any.

    ``None`` means the failure is in the stdlib/interpreter itself (or can't be
    attributed), i.e. a genuinely broken Python. A third-party module means the
    user's environment holds a missing, stale, or conflicting copy of one of
    our dependencies — typically left behind by another tool sharing the same
    global interpreter — and reinstalling Python would not help at all.
    """
    import re

    module = getattr(exc, "name", None)
    if not module:
        # "cannot import name 'X' from 'pkg.sub' (/path/...)"
        m = re.search(r"from '([\w.]+)'", str(exc))
        module = m.group(1) if m else None
    if not module:
        return None
    top = module.split(".")[0]
    if top == "velune" or top.startswith("_") or top in sys.stdlib_module_names:
        return None
    try:
        from importlib.metadata import packages_distributions

        return packages_distributions().get(top, [top])[0]
    except Exception:
        return top


def _fatal_environment_error(exc: BaseException) -> None:
    """Print an actionable message for a broken Python/runtime, then exit.

    Reached when a top-level import fails. Two very different causes share
    this path, so the message is chosen by which module failed:

    * a third-party dependency (``cannot import name 'X' from 'typer'``) —
      the environment has an incompatible/missing copy of one of our deps;
      the fix is upgrading or isolating Velune, never reinstalling Python;
    * the stdlib/interpreter (``DLL load failed while importing _ctypes`` on
      Windows after a Python upgrade/uninstall) — Python itself is broken.

    We deliberately do *not* show a raw traceback: it confuses
    non-developers and buries the fix.

    Note: this cannot catch the ``velune.exe`` launcher failing to locate
    ``pythonXY.dll`` (Windows error 126) — that happens in the C launcher
    *before* any Python runs. The remedy for that case is the same and is
    printed here so it is discoverable via ``python -m velune``.
    """
    dist = _failing_dependency(exc)
    if dist is not None:
        try:
            from importlib.metadata import version

            installed = f"version {version(dist)} is installed"
        except Exception:
            installed = "it is not installed"
        msg = (
            f"Velune could not start because its dependency '{dist}' is missing or "
            f"incompatible ({installed}).\n\n"
            f"  Underlying error: {type(exc).__name__}: {exc}\n\n"
            "This usually means another tool in the same Python environment\n"
            "installed a different version of that package.\n\n"
            "How to fix:\n"
            "  1. Upgrade Velune and its dependencies in this Python:\n"
            f'       "{sys.executable}" -m pip install --upgrade velune-cli\n'
            "  2. Or install Velune in its own isolated environment, so no other\n"
            "     tool can change its dependencies (recommended):\n"
            "       pipx install velune-cli     (or: uv tool install velune-cli)\n"
        )
        sys.stderr.write("\n" + msg + "\n")
        raise SystemExit(1)

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


def _install_crash_hook() -> None:
    """Redact secrets from any exception that reaches Python's default excepthook.

    Typer's own pretty-exception renderer is told not to show local variables
    (see ``cli/app.py``), which closes the main leak vector — a decrypted
    provider key sitting in a local when a command callback crashes. This is
    a backstop for exceptions that never reach Typer's renderer at all, e.g.
    ones raised while parsing argv or importing ``cli.app`` itself, which
    would otherwise print through Python's default hook unredacted.
    """

    def _hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        import traceback

        from velune.core.redaction import redact_secrets

        rendered = "".join(traceback.format_exception(exc_type, exc, tb))  # type: ignore[arg-type]
        sys.stderr.write(redact_secrets(rendered))

    sys.excepthook = _hook


def _force_utf8_stdio() -> None:
    """Ensure stdout/stderr can carry the UI's non-ASCII characters.

    On Windows, ``sys.stdout.encoding`` defaults to the active ANSI code page
    (typically cp1252). Velune's output is full of box-drawing glyphs, arrows,
    and em-dashes; under cp1252 an em-dash silently encodes to byte 0x97
    instead of UTF-8, which renders as a replacement character in any UTF-8
    terminal — the ``velune --help`` output showed "a project <?> config"
    for exactly this reason.

    ``reconfigure`` is a no-op where the encoding is already UTF-8, and is
    wrapped defensively because stdout may be replaced by a stream that does
    not support it (pytest capture, some embedding hosts).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            # Non-reconfigurable stream — leave it exactly as it was.
            pass


def main() -> None:
    """Console-script entry point.

    Fast-paths ``--version`` (the canonical install smoke test) without
    importing ``velune.cli`` or any subsystem, then delegates everything else
    to the lazily-built Typer application.
    """
    _force_utf8_stdio()
    _install_crash_hook()
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
    value_opts = {"-w", "--workspace", "-c", "--config", "--mode"}
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

            render_root_help()
        except ImportError as exc:
            _fatal_environment_error(exc)
        raise SystemExit(0)

    try:
        from velune.cli.app import create_app

        # No subcommand → the bare interactive REPL, which needs zero
        # subcommand modules imported. Otherwise register just the invoked
        # command. Building the app is inside the guard because create_app()
        # and the command module import core deps (rich, typer…) lazily.
        cli = create_app(register=subcommand)
    except ImportError as exc:
        # A failed import while *building* the CLI means the interpreter or a
        # core dependency is unusable. Surface an actionable message instead
        # of a cryptic traceback / Windows DLL popup. Errors raised while a
        # command *runs* are handled by Typer and intentionally not caught here.
        _fatal_environment_error(exc)

    cli()


def __getattr__(name: str):
    # Lazy attribute access keeps `from velune.main import app` working for
    # backward compatibility without building the app at import time.
    if name == "app":
        from velune.cli.app import app as _app

        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    main()
