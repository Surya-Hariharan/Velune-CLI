"""Tests for JobStatus, JobRecord, and JobRegistry."""

from __future__ import annotations

import asyncio
import time

import pytest

from velune.core.task_registry import JobRecord, JobRegistry, JobStatus


class TestJobRegistry:
    def setup_method(self):
        self.registry = JobRegistry()

    def test_new_id_is_sequential(self):
        id1 = self.registry.new_id()
        id2 = self.registry.new_id()
        id3 = self.registry.new_id()
        assert id1 == "job-0001"
        assert id2 == "job-0002"
        assert id3 == "job-0003"

    def test_register_and_get(self):
        job = JobRecord(job_id="job-0001", name="test task")
        self.registry.register(job)
        result = self.registry.get("job-0001")
        assert result is not None
        assert result.name == "test task"
        assert result.status == JobStatus.PENDING

    def test_get_nonexistent_returns_none(self):
        assert self.registry.get("job-9999") is None

    def test_update_status(self):
        job = JobRecord(job_id="job-0001", name="task")
        self.registry.register(job)
        self.registry.update("job-0001", status=JobStatus.RUNNING)
        assert self.registry.get("job-0001").status == JobStatus.RUNNING

    def test_update_multiple_fields(self):
        job = JobRecord(job_id="job-0001", name="task")
        self.registry.register(job)
        self.registry.update("job-0001", current_phase="planning", result_preview="done")
        got = self.registry.get("job-0001")
        assert got.current_phase == "planning"
        assert got.result_preview == "done"

    def test_active_count_tracks_pending_and_running(self):
        for i in range(4):
            job = JobRecord(job_id=f"job-{i:04d}", name=f"task {i}")
            self.registry.register(job)

        assert self.registry.active_count() == 4

        self.registry.update("job-0000", status=JobStatus.COMPLETED)
        self.registry.update("job-0001", status=JobStatus.FAILED)
        assert self.registry.active_count() == 2

    def test_all_jobs_returns_all(self):
        for i in range(3):
            self.registry.register(JobRecord(job_id=f"job-{i:04d}", name=f"task {i}"))
        assert len(self.registry.all_jobs()) == 3

    def test_cancel_sets_cancelled_status(self):
        job = JobRecord(job_id="job-0001", name="task")
        self.registry.register(job)
        result = self.registry.cancel("job-0001")
        assert result is True
        assert self.registry.get("job-0001").status == JobStatus.CANCELLED

    def test_cancel_nonexistent_returns_false(self):
        assert self.registry.cancel("job-9999") is False

    @pytest.mark.asyncio
    async def test_cancel_cancels_asyncio_task(self):
        cancelled = asyncio.Event()

        async def long_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        job = JobRecord(job_id="job-0001", name="long")
        self.registry.register(job)
        task = asyncio.create_task(long_task())
        self.registry.update("job-0001", task=task, status=JobStatus.RUNNING)

        # Let the task start and reach its first await before cancelling
        await asyncio.sleep(0)
        self.registry.cancel("job-0001")
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert cancelled.is_set()

    def test_submitted_at_is_set_automatically(self):
        before = time.monotonic()
        job = JobRecord(job_id="job-0001", name="task")
        after = time.monotonic()
        assert before <= job.submitted_at <= after
