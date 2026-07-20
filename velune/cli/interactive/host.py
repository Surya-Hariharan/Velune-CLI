"""The currently-installed host for palette-styled interactive steps.

There are two ways to run a step like "pick a provider": stand up a private
``prompt_toolkit`` ``Application`` for it (``runner.run_standalone`` — right for
``velune setup`` and any other non-REPL entry point), or draw it into an
Application that is *already* running (``inline_flow.InlineFlow`` — right inside
the REPL, where a second Application would render below the prompt box and make
a single flow hop around the terminal between steps).

Which one applies is a property of *where the process currently is*, not of the
flow being run: ``ProviderPalette`` is the same code either way. So the REPL
installs itself as the host on entry and removes itself on exit, and the
helpers in ``velune.cli.interactive`` consult this module rather than taking a
host parameter that every call site would have to thread through.

A module-level slot, not a ``contextvars.ContextVar``: the REPL's dispatch loop
and its UI Application run as sibling ``asyncio.Task``s, which get independent
copies of the context, so a ContextVar set by one is invisible to the other.
"""

from __future__ import annotations

from typing import Any

_host: Any | None = None


def install(host: Any) -> None:
    """Route palette-styled steps through *host* until ``uninstall()``."""
    global _host
    _host = host


def uninstall(host: Any) -> None:
    """Remove *host*, if it is still the installed one.

    Identity-guarded so a late teardown cannot clear a host that something else
    has since installed.
    """
    global _host
    if _host is host:
        _host = None


def active() -> Any | None:
    """The installed host, or None to mean "run standalone"."""
    return _host
