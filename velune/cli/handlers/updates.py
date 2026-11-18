"""REPL side of ask-first automatic updates (see :mod:`velune.lifecycle.autoupdate`).

Start: report the last post-exit update and check in the background (never prints).
Between prompts: ask about a found release once. Exit: start the post-exit updater.
Every failure is silent: an update check must never break a session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.markup import escape

from velune.cli import design
from velune.lifecycle import autoupdate

_log = logging.getLogger("velune.cli.updates")


def _settings(repl: Any) -> tuple[str, float]:
    update = getattr(repl.container.get_optional("runtime.config"), "update", None)
    return str(getattr(update, "auto", "ask")), float(getattr(update, "check_interval_hours", 24))


def _run(repl: Any, cmd: str | None) -> None:
    if cmd:
        repl.console.print(f"  Run: [bold]{escape(cmd)}[/bold]")


def report_last_result(repl: Any) -> None:
    try:
        result = autoupdate.take_last_result()
        if not result:
            return
        message = escape(str(result.get("message") or ""))
        if result.get("status") == "updated":
            repl.console.print(f"[{design.OK}]✓[/{design.OK}] {message}")
        else:
            repl.console.print(f"[{design.WARN}]Auto-update:[/{design.WARN}] {message}")
            _run(repl, result.get("manual_command"))
    except Exception as exc:
        _log.debug("Could not report the last update (non-fatal): %s", exc)


async def background_check(repl: Any) -> None:
    try:
        mode, interval = _settings(repl)
        if autoupdate.due(mode, interval, await asyncio.to_thread(autoupdate.load_state)):
            repl._update_offer = await asyncio.to_thread(autoupdate.check_for_update)
    except Exception as exc:
        _log.debug("Update check failed (non-fatal): %s", exc)


async def maybe_ask(repl: Any) -> None:
    offer, repl._update_offer = getattr(repl, "_update_offer", None), None
    if offer is None:
        return
    try:
        from velune.execution.approval import ask_choice

        headline = f"Velune {offer.latest} is available (you have {offer.installed})."
        if _settings(repl)[0] == "notify" or offer.plan is None:
            repl.console.print(f"[{design.ACCENT}]↑[/{design.ACCENT}] {escape(headline)}")
            if offer.guidance:
                repl.console.print(f"  {escape(offer.guidance)}")
            _run(repl, offer.manual_command)
            return
        answer = await ask_choice(
            f"{headline} Install it when you exit Velune?",
            [
                ("yes", "Yes, update when I exit"),
                ("no", f"No, skip {offer.latest}"),
                ("later", "Ask me next time"),
            ],  # fmt: skip
            default="yes",
            reject="later",
        )
        if answer == "yes" and await asyncio.to_thread(autoupdate.accept, offer):
            repl.console.print(
                f"[{design.OK}]✓[/{design.OK}] Velune {escape(offer.latest)} will be installed "
                "after you exit (once every Velune window is closed)."
            )
        elif answer == "no":
            await asyncio.to_thread(autoupdate.decline, offer)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.debug("Update prompt failed (non-fatal): %s", exc)


def on_exit() -> None:
    autoupdate.spawn_pending()  # never raises
