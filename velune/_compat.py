"""Standard-library compatibility shims for older supported Pythons.

Velune supports Python 3.10+. A few stdlib conveniences we rely on only landed
in 3.11; this module backfills them so the rest of the codebase can import a
single, version-agnostic name.

Currently provides:
    StrEnum        — :class:`enum.StrEnum` on 3.11+, a behaviourally-equivalent
                     ``str``/``Enum`` mixin on 3.10.
    uncancel_task  — :meth:`asyncio.Task.uncancel` where available (3.11+),
                     a no-op on 3.10.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - exercised only on Python 3.10
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport of :class:`enum.StrEnum` for Python 3.10.

        Reproduces the two behaviours the codebase depends on:

        * ``str(Member)`` returns the member's *value* (not ``"Class.MEMBER"``),
          matching the real ``StrEnum`` and plain string formatting.
        * ``auto()`` yields the lower-cased member name, exactly as the 3.11
          implementation does.
        """

        __str__ = str.__str__

        @staticmethod
        def _generate_next_value_(name, start, count, last_values):  # noqa: N805
            return name.lower()


def uncancel_task(task: asyncio.Task) -> None:
    """Decrement a task's cancellation count where supported.

    :meth:`asyncio.Task.uncancel` arrived in Python 3.11 to balance an
    ``uncancel`` against a prior ``cancel`` (the 3.11 cancellation counter).
    Python 3.10 has no such counter, so skipping the call is the correct,
    equivalent behaviour rather than an error.
    """
    uncancel = getattr(task, "uncancel", None)
    if uncancel is not None:
        uncancel()


__all__ = ["StrEnum", "uncancel_task"]
