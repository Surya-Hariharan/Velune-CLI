"""Tests for the persistent trace log and the bus → log sink.

These verify the observability stream is real and safe: events round-trip
through the JSONL store, secrets are redacted at write time, the file is bounded,
filters work, and the sink persists genuinely-emitted bus events without
fabricating any.
"""

from __future__ import annotations

from velune.observability.trace_log import TraceLog


def _evt(i, etype="Test", source="planner", corr=None, data=None):
    return {
        "event_id": f"evt-{i}",
        "event_type": etype,
        "timestamp": 1_700_000_000.0 + i,
        "source": source,
        "correlation_id": corr,
        "data": data or {},
    }


class TestTraceLog:
    def test_append_and_read_roundtrip(self, tmp_path):
        log = TraceLog(tmp_path / "trace.jsonl")
        log.append(_evt(1, etype="Indexed"))
        log.append(_evt(2, etype="Executed"))
        records = log.read_recent(limit=10)
        assert [r["event_type"] for r in records] == ["Indexed", "Executed"]
        assert log.count() == 2

    def test_read_recent_returns_tail(self, tmp_path):
        log = TraceLog(tmp_path / "trace.jsonl")
        for i in range(10):
            log.append(_evt(i))
        recent = log.read_recent(limit=3)
        assert [r["event_id"] for r in recent] == ["evt-7", "evt-8", "evt-9"]

    def test_type_filter_is_case_insensitive_substring(self, tmp_path):
        log = TraceLog(tmp_path / "trace.jsonl")
        log.append(_evt(1, etype="MemoryRetrieved"))
        log.append(_evt(2, etype="SandboxApproved"))
        out = log.read_recent(type_filter="memory")
        assert len(out) == 1
        assert out[0]["event_type"] == "MemoryRetrieved"

    def test_run_id_filter(self, tmp_path):
        log = TraceLog(tmp_path / "trace.jsonl")
        log.append(_evt(1, corr="run-A"))
        log.append(_evt(2, corr="run-B"))
        out = log.read_recent(run_id="run-A")
        assert len(out) == 1
        assert out[0]["correlation_id"] == "run-A"

    def test_secrets_redacted_at_write(self, tmp_path):
        log = TraceLog(tmp_path / "trace.jsonl")
        secret = "sk-ant-" + "A" * 40
        log.append(_evt(1, data={"prompt": f"key is {secret}"}))
        # Read the raw file: the secret must not be present on disk.
        raw = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
        assert secret not in raw
        assert "REDACTED" in raw

    def test_log_is_bounded(self, tmp_path):
        log = TraceLog(tmp_path / "trace.jsonl", max_entries=10)
        for i in range(40):
            log.append(_evt(i))
        # Trim keeps the count at or below the cap once it overshoots.
        assert log.count() <= 10
        # And it keeps the most recent entries.
        ids = [r["event_id"] for r in log.read_recent(limit=10)]
        assert ids[-1] == "evt-39"

    def test_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / "trace.jsonl"
        log = TraceLog(path)
        log.append(_evt(1))
        with open(path, "a", encoding="utf-8") as f:
            f.write("not json\n")
        log.append(_evt(2))
        records = log.read_recent()
        assert [r["event_id"] for r in records] == ["evt-1", "evt-2"]

    def test_clear(self, tmp_path):
        log = TraceLog(tmp_path / "trace.jsonl")
        log.append(_evt(1))
        log.clear()
        assert log.count() == 0
        assert log.read_recent() == []

    def test_read_missing_file_is_empty(self, tmp_path):
        log = TraceLog(tmp_path / "nope.jsonl")
        assert log.read_recent() == []
        assert log.count() == 0


class TestTraceSink:
    async def test_sink_persists_emitted_events(self, tmp_path):
        from velune.events import CognitiveBus, Event
        from velune.observability.trace_sink import TraceSink

        log = TraceLog(tmp_path / "trace.jsonl")
        sink = TraceSink(log)
        bus = CognitiveBus()
        await sink.attach(bus)

        await bus.emit(Event(event_type="PlannerStarted", source="planner", data={"task": "demo"}))
        await bus.emit(Event(event_type="ExecutorRan", source="executor", data={"cmd": "pytest"}))

        records = log.read_recent()
        types = [r["event_type"] for r in records]
        assert "PlannerStarted" in types
        assert "ExecutorRan" in types

        await sink.detach()
        # After detach, new events are no longer persisted.
        before = log.count()
        await bus.emit(Event(event_type="AfterDetach", source="planner"))
        assert log.count() == before


class TestRecordMilestone:
    def test_records_phase_event_shape(self, tmp_path):
        from velune.observability.trace_sink import record_milestone

        log = TraceLog(tmp_path / "trace.jsonl")
        record_milestone(log, "run-xyz", 1, "planner", "Drafting a plan")
        record_milestone(log, "run-xyz", 2, "", "Unlabeled progress line")

        records = log.read_recent()
        assert records[0]["event_type"] == "council.planner"
        assert records[0]["source"] == "planner"
        assert records[0]["correlation_id"] == "run-xyz"
        assert records[0]["data"]["message"] == "Drafting a plan"
        # Empty phase falls back to a generic council/progress tag.
        assert records[1]["event_type"] == "council.progress"
        assert records[1]["source"] == "council"

    def test_milestones_are_run_correlated(self, tmp_path):
        from velune.observability.trace_sink import record_milestone

        log = TraceLog(tmp_path / "trace.jsonl")
        record_milestone(log, "run-A", 1, "planner", "a")
        record_milestone(log, "run-B", 1, "coder", "b")
        only_a = log.read_recent(run_id="run-A")
        assert len(only_a) == 1
        assert only_a[0]["source"] == "planner"


class TestTraceSinkRedaction:
    async def test_sink_redacts_secrets(self, tmp_path):
        from velune.events import CognitiveBus, Event
        from velune.observability.trace_sink import TraceSink

        log = TraceLog(tmp_path / "trace.jsonl")
        sink = TraceSink(log)
        bus = CognitiveBus()
        await sink.attach(bus)

        secret = "sk-ant-" + "Z" * 40
        await bus.emit(Event(event_type="X", source="planner", data={"k": secret}))

        raw = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
        assert secret not in raw
