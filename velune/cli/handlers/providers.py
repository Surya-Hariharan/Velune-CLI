"""Provider management slash command handler: /providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.providers")


async def cmd_providers(repl: VeluneREPL, args: str) -> None:
    """Open the interactive provider management palette.

    Sub-commands (all optional):
      /providers                — open the interactive palette
      /providers add [id]       — connect a cloud provider
      /providers manage [id]    — view / update / remove a provider
      /providers test [id]      — test connection(s)
      /providers discover       — re-scan models from all providers
      /providers refresh        — alias for discover
      /providers remove <id>    — remove a provider's API key
      /providers status         — print a status table for every provider
    """
    from velune.cli.provider_ui import ProviderPalette

    palette = ProviderPalette(console=repl.console, container=repl.container)
    await palette.run(args)
