"""The ``orchestration.engine`` feature flag.

Default ``"off"``. Since Phase 4E-6 the only consumer outside the engine is the explicit ``velune
native`` command (see ``velune/cli/commands/native.py``); chat, the REPL, ``ask``, ``run``, the
Council and MCP never consult it. A config object without an ``orchestration`` section (older
objects, test doubles) reads as off.
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
