"""Bindings between the domain-neutral core and the rest of Velune.

``runtime`` is the only module in ``velune.council`` that touches providers, models or the legacy
council. ``legacy`` is a pure mapper over plain dicts. Nothing here is imported by the core.
"""

from __future__ import annotations
