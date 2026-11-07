"""Wording for "reinstall Velune" advice, chosen by how this copy was installed.

Standard library only: it is used from the start-up failure path in
``velune.main``, where third-party imports may be exactly what is broken.

This is a **hint**, not an ownership decision. It only looks for uv's receipt
file in the running environment so the advice can name ``uv`` instead of
``pip``. Anything that mutates an installation must use
:func:`velune.lifecycle.ownership.detect_ownership`, which requires much more
evidence.
"""

from __future__ import annotations

import os
import sys

REINSTALL_COMMAND = "uv tool install --force velune-cli"


def is_uv_tool_environment(prefix: str | None = None) -> bool:
    """True if the running environment contains a ``uv-receipt.toml`` (advice only)."""
    try:
        return os.path.isfile(os.path.join(prefix or sys.prefix, "uv-receipt.toml"))
    except (OSError, ValueError):
        return False


def uv_reinstall_advice(platform: str | None = None) -> str:
    """One sentence on how to reinstall a uv-managed Velune safely."""
    advice = (
        f"Reinstall it with: {REINSTALL_COMMAND}   "
        '(add your extras if you use any, e.g. "velune-cli[rag]")'
    )
    if (platform or sys.platform) == "win32":
        advice += ". Close Velune first and run this in a fresh terminal."
    return advice
