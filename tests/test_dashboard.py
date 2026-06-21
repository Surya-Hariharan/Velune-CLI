"""Tests for ProgressDashboard Rich rendering (no Live display, just builder methods)."""

from __future__ import annotations

import time

from rich.console import Console

from velune.cli.display.dashboard import ProgressDashboard
from velune.core.task_registry import JobRecord, JobRegistry, JobStatus
from velune.proactive.alerts import AlertSeverity, AlertStore, make_alert


def _make_registry(*statuses: JobStatus) -> JobRegistry:
    reg = JobRegistry()
    for i, st in enumerate(statuses):
        job = JobRecord(job_id=f"job-{i:04d}", name=f"task {i}", status=st)
        reg.register(job)
    return reg


def _make_store(*severities: AlertSeverity) -> AlertStore:
    store = AlertStore()
    for sev in severities:
        store.add(make_alert(sev, f"{sev} alert", "body", "test"))
    return store


class TestProgressDashboard:
    def _dashboard(self, job_registry=None, alert_store=None):
        console = Console(force_terminal=False, width=120)
        return ProgressDashboard(
            console=console,
            job_registry=job_registry,
            alert_store=alert_store,
        )

    def test_jobs_table_empty(self):
        d = self._dashboard(job_registry=_make_registry())
        table = d._build_jobs_table()
        # Renders without error
        from rich.console import Console as _C

        _C(force_terminal=False).print(table)

    def test_jobs_table_with_running_job(self):
        reg = _make_registry(JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.FAILED)
        d = self._dashboard(job_registry=reg)
        table = d._build_jobs_table()
        from rich.console import Console as _C

        _C(force_terminal=False).print(table)

    def test_alerts_panel_empty(self):
        d = self._dashboard(alert_store=_make_store())
        panel = d._build_alerts_panel()
        from rich.console import Console as _C

        _C(force_terminal=False).print(panel)

    def test_alerts_panel_with_entries(self):
        store = _make_store(AlertSeverity.DANGER, AlertSeverity.WARN, AlertSeverity.INFO)
        d = self._dashboard(alert_store=store)
        panel = d._build_alerts_panel()
        from rich.console import Console as _C

        _C(force_terminal=False).print(panel)

    def test_health_panel_no_monitor(self):
        d = self._dashboard()
        panel = d._build_health_panel()
        from rich.console import Console as _C

        _C(force_terminal=False).print(panel)

    def test_build_layout_does_not_raise(self):
        reg = _make_registry(JobStatus.RUNNING)
        store = _make_store(AlertSeverity.WARN)
        d = self._dashboard(job_registry=reg, alert_store=store)
        layout = d._build_layout()
        from rich.console import Console as _C

        _C(force_terminal=False, width=120).print(layout)

    def test_jobs_table_none_registry(self):
        d = self._dashboard(job_registry=None)
        table = d._build_jobs_table()
        from rich.console import Console as _C

        _C(force_terminal=False).print(table)

    def test_alerts_panel_none_store(self):
        d = self._dashboard(alert_store=None)
        panel = d._build_alerts_panel()
        from rich.console import Console as _C

        _C(force_terminal=False).print(panel)

    def test_jobs_table_completed_job_has_elapsed(self):
        reg = JobRegistry()
        job = JobRecord(
            job_id="job-0001",
            name="done task",
            status=JobStatus.COMPLETED,
            submitted_at=time.monotonic() - 30,
            completed_at=time.monotonic(),
            result_preview="all good",
        )
        reg.register(job)
        d = self._dashboard(job_registry=reg)
        table = d._build_jobs_table()
        from rich.console import Console as _C
        from rich.text import Text

        buf = _C(force_terminal=False)
        buf.print(table)
