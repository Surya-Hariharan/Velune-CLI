"""Result sentinels shared by every interactive widget.

``BACK``/``CANCEL`` are distinguishable by identity from any real value a
widget can produce (including ``None``, ``False``, or an empty list from a
multi-select with nothing checked) — overloading ``None`` as "cancelled" is
exactly the ambiguity this module exists to retire.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TypeVar

T = TypeVar("T")


class _Control(Enum):
    BACK = auto()
    CANCEL = auto()


BACK = _Control.BACK
CANCEL = _Control.CANCEL

WidgetResult = T | _Control
