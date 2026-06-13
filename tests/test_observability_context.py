"""Tests for the `velune context` report builder and shared formatters.

Every assertion checks that the builder reports *real* on-disk state truthfully:
counts derive from a written index_state, freshness reflects the git comparison,
and an absent index yields an honest empty report rather than fabricated data.
"""

from __future__ import annotations

import json
import sqlite3

from velune.observability.context_report import build_context_report
from velune.observability.format import human_bytes, relative_time


def _write_index_state(workspace, files, commit_sha=None, indexed_at=1_700_000_000.0):
    """Write a minimal real index_state.json into workspace/.velune."""
    velune_dir = workspace / ".velune"
    velune_dir.mkdir(parents=True, exist_ok=True)
    file_index = {
        rel: {
            "path": rel,
            "content_hash": "deadbeef",
            "language": lang,
            "symbol_count": syms,
            "indexed_at": indexed_at,
        }
        for rel, lang, syms in files
    }
    data = {
        "workspace_root": str(workspace),
        "last_commit_sha": commit_sha,
        "last_indexed_at": indexed_at,
        "file_index": file_index,
    }
    (velune_dir / "index_state.json").write_text(json.dumps(data), encoding="utf-8")


class TestFormatters:
    def test_human_bytes_scales(self):
        assert human_bytes(0) == "0 B"
        assert human_bytes(512) == "512 B"
        assert human_bytes(1536) == "1.5 KB"
        assert human_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_relative_time_buckets(self):
        now = 1_000_000.0
        assert relative_time(None, now=now) == "never"
        assert relative_time(0, now=now) == "never"
        assert relative_time(now - 30, now=now) == "just now"
        assert relative_time(now - 300, now=now) == "5m ago"
        assert relative_time(now - 7200, now=now) == "2h ago"
        assert relative_time(now - 3 * 86400, now=now) == "3d ago"


class TestContextReport:
    def test_no_index_is_honest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path / "data"))
        report = build_context_report(tmp_path)
        assert report.index_exists is False
        assert report.indexed_file_count == 0
        assert report.total_symbols == 0
        assert report.freshness == "no-index"
        assert any(state == "warn" for state, _ in report.health)

    def test_counts_languages_and_areas(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path / "data"))
        _write_index_state(
            tmp_path,
            [
                ("velune/a.py", "python", 10),
                ("velune/b.py", "python", 5),
                ("docs/c.md", "markdown", 2),
            ],
        )
        report = build_context_report(tmp_path)
        assert report.index_exists is True
        assert report.indexed_file_count == 3
        assert report.total_symbols == 17
        # Languages sorted by file count, python first.
        assert report.languages[0].language == "python"
        assert report.languages[0].files == 2
        assert report.languages[0].symbols == 15
        # Top areas grouped by first path segment.
        areas = dict(report.top_areas)
        assert areas["velune"] == 2
        assert areas["docs"] == 1

    def test_freshness_unknown_without_git(self, tmp_path, monkeypatch):
        # tmp_path is not a git repo, so HEAD cannot be resolved.
        monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path / "data"))
        _write_index_state(tmp_path, [("velune/a.py", "python", 1)], commit_sha="abc123")
        report = build_context_report(tmp_path)
        assert report.head_sha is None
        assert report.freshness == "unknown"

    def test_memory_tables_read_real_sqlite(self, tmp_path, monkeypatch):
        data_home = tmp_path / "data"
        monkeypatch.setenv("VELUNE_DATA_HOME", str(data_home))
        _write_index_state(tmp_path, [("velune/a.py", "python", 1)])

        # Create the cognitive DB exactly where the report will look for it.
        from velune.core.paths import cognitive_db_path

        db_path = cognitive_db_path(tmp_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO turns DEFAULT VALUES", [()] * 3)
        conn.commit()
        conn.close()

        report = build_context_report(tmp_path)
        tables = {t.table: t.rows for t in report.memory_tables}
        assert tables.get("turns") == 3

    def test_to_dict_is_json_serializable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path / "data"))
        _write_index_state(tmp_path, [("velune/a.py", "python", 1)])
        report = build_context_report(tmp_path)
        # Round-trips without raising.
        encoded = json.dumps(report.to_dict())
        assert "indexed_file_count" in encoded
