"""Domain-neutral Council core: contracts, profiles, stages and a stage-agnostic runner.

This package is the *reasoning* layer. It cannot execute, edit, ask permission or render
anything: Manual / Plan / Auto (execution authority) live elsewhere and are untouched.
Provider, model and trace access happen only behind the ports in ``velune.council.ports``,
bound to the real runtime by ``velune.council.adapters``.

``velune.cognition.council`` is the separate, existing coding pipeline.
"""

from __future__ import annotations
