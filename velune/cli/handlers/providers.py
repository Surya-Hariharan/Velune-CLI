"""Provider management slash command handlers: /providers and /connect."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.providers")


def _palette(repl: VeluneREPL):
    from velune.cli.provider_ui import ProviderPalette

    return ProviderPalette(console=repl.console, container=repl.container)


async def cmd_providers(repl: VeluneREPL, args: str) -> None:
    """Open the interactive provider management palette.

    Sub-commands (all optional):
      /providers                — open the interactive palette
      /providers add [id]       — connect a cloud provider
      /providers manage [id]    — view / update / remove a provider
      /providers test [id]      — re-verify connection(s)
      /providers discover       — re-scan models from all providers
      /providers remove <id>    — remove a provider's API key
      /providers status         — status table for every provider
    """
    await _palette(repl).run(args)


async def cmd_login(repl: VeluneREPL, args: str) -> None:
    """Connect a provider: pick one, paste the key, watch it verify.

    The shortest path to "I want to paste a key" — it lands directly on the
    provider picker rather than a management menu. ``/connect anthropic`` skips
    the picker entirely.
    """
    target = args.strip().split(None, 1)[0].lower() if args.strip() else ""
    await _palette(repl).run(f"add {target}".strip())
