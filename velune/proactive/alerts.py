"""Alert data model and bounded store for proactive issue surfacing."""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class AlertSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    DANGER = "danger"


@dataclass
class Alert:
    alert_id: str
    severity: AlertSeverity
    title: str
    body: str
    source: str
    created_at: float = field(default_factory=time.monotonic)
    acknowledged: bool = False


def make_alert(severity: AlertSeverity, title: str, body: str, source: str) -> Alert:
    return Alert(
        alert_id=uuid.uuid4().hex[:8],
        severity=severity,
        title=title,
        body=body,
        source=source,
    )


class AlertStore:
    """Bounded LIFO store of active alerts. Thread-safe for single-writer / multi-reader use."""

    MAX_ALERTS: int = 20

    def __init__(self) -> None:
        self._alerts: deque[Alert] = deque(maxlen=self.MAX_ALERTS)
        self._unread: int = 0

    def add(self, alert: Alert) -> None:
        self._alerts.appendleft(alert)
        self._unread += 1

    def drain_unread(self) -> list[Alert]:
        """Return all unread alerts and mark them acknowledged."""
        unread = [a for a in self._alerts if not a.acknowledged]
        for a in unread:
            a.acknowledged = True
        self._unread = 0
        return unread

    def all_alerts(self) -> list[Alert]:
        return list(self._alerts)

    def unread_count(self) -> int:
        return self._unread

    def clear(self) -> None:
        self._alerts.clear()
        self._unread = 0
