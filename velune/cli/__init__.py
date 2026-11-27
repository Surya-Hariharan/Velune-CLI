"""CLI entry points.

Import the Typer app with ``from velune.cli.app import app``. This package stays empty on
purpose: importing a light submodule (``velune.cli.registry`` for fast top-level help, or
``velune.cli.design`` for styling) must not pull in the whole Typer application.
"""
