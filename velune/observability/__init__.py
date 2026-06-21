"""Operational-transparency surfaces for Velune.

This package turns Velune's internal runtime state into *inspectable*, truthful
reports so operators can verify the system is actually doing what it claims:

* :mod:`velune.observability.context_report` proves repository indexing,
  context persistence, and freshness from real on-disk state.
* :mod:`velune.observability.trace_log` and
  :mod:`velune.observability.trace_sink` persist and replay the real execution
  event stream emitted on the :class:`~velune.events.CognitiveBus`.

Nothing here fabricates statistics: every number is read from a real file,
database, or emitted event. When data is absent, the report says so rather than
inventing a placeholder.
"""

from __future__ import annotations
