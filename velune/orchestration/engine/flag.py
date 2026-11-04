"""The ``orchestration.engine`` feature flag.

Default ``"off"``. Phase 4B only defines and reads the flag; no CLI, REPL or MCP path consults it,
so setting it to ``"on"`` changes nothing yet. A config object without an ``orchestration`` section
(older objects, test doubles) reads as off.
"""

from __future__ import annotations

from typing import Any

ENGINE_OFF = "off"
ENGINE_ON = "on"


def engine_mode(config: Any) -> str:
    mode = getattr(getattr(config, "orchestration", None), "engine", ENGINE_OFF)
    return ENGINE_ON if mode == ENGINE_ON else ENGINE_OFF


def engine_enabled(config: Any) -> bool:
    return engine_mode(config) == ENGINE_ON
