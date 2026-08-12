"""Provider slash command handler: ``/connect`` — the only provider surface.

``/providers`` (and its ``/provider`` / ``/prov`` aliases) used to live here
too, wrapping the same palette in a management menu. It was removed because it
duplicated this entry point: two commands both meant "connect a provider" and
neither was canonical. Its management-only operations (discover / refresh /
test / remove / status) were preserved, not deleted — they are served by the
``velune provider ...`` CLI in ``cli/commands/providers.py``, which already
implemented each of them against the same provider subsystem.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.providers")


def _palette(repl: VeluneREPL):
    from velune.cli.provider_ui import ProviderPalette

    return ProviderPalette(console=repl.console, container=repl.container)


async def cmd_login(repl: VeluneREPL, args: str) -> None:
    """Connect a provider: pick one, paste the key, watch it verify.

    The shortest path to "I want to paste a key" — it lands directly on the
    provider picker rather than a management menu. ``/connect anthropic`` skips
    the picker entirely.

    ``/connect``'s grammar is a single optional provider-id argument. Anything
    beyond a recognized id — or the whole string, if the first token isn't a
    provider id at all — is not part of the command; it's carried through as
    the user's leftover message and handed back to the prompt once the
    connect flow finishes (mirroring ``/model``'s mixed-input handling), the
    same way an unmatched free-text token must never be silently dropped or
    misread as an unknown-provider error.
    """
    from velune.providers import catalog

    stripped = args.strip()
    if not stripped:
        await _palette(repl).run()
        return

    parts = stripped.split(None, 1)
    candidate = parts[0].lower()
    leftover = parts[1].strip() if len(parts) > 1 else ""

    if catalog.get(candidate) is not None:
        await _palette(repl).run(candidate)
    else:
        leftover = stripped
        await _palette(repl).run()

    if leftover:
        repl._pending_prompt_text = leftover
